/**
 * Music Assistant Podcasts — episodes card
 *
 * Auto-registered by the music_assistant_podcasts integration:
 *   served as a versioned Lovelace resource by the integration
 *
 * YAML config (or use the built-in editor via the card picker):
 *   type: custom:music-assistant-podcasts-card
 *   entity: sensor.latest_podcast_episodes   # (optional, auto-detected)
 *   player: media_player.x                   # (optional, overrides the integration default)
 *   title: Legfrissebb epizódok
 *   max_items: 20
 *   show_played: true
 *
 * Layout per row:
 *   row 1 (info):  podcast name · published · length · "még X perc"
 *   row 2:         episode title (click = toggles a scrollable description
 *                  panel under the row, one open at a time; seamless marquee
 *                  on hover when it does not fit)
 *
 * Extras: the cover image is the play trigger (hover overlay; on touch a
 * tap starts playback) with a short "starting playback" toast on the row;
 * podcast name links to the MA web UI; progress bars update live via the
 * progress endpoint.
 */

const CARD_NAME = "music-assistant-podcasts-card";
// Adaptive progress polling: fast while something is playing, slow otherwise.
const PROGRESS_POLL_ACTIVE_SECONDS = 10;
const PROGRESS_POLL_IDLE_SECONDS = 60;
// Extra polls right after a play click (provider resume points can lag).
const PROGRESS_BURST_DELAYS_MS = [2000, 6000, 12000];
// After a play click the episode is optimistically shown as in progress for
// this long (and a premature "finished" from MA is distrusted until a poll
// arrives after the grace window).
const PLAY_GRACE_MS = 45000;
// description panel: natural height up to this, then internal scrolling
const DESC_PANEL_MAX_HEIGHT = 300;

// Sample rows shown when no sensor entity is configured (card picker preview)
const DEMO_EPISODES = [
  {
    title: "248. rész – Csak azért is, mert mindenki fél tőle",
    podcast: "HetiVálasz Podcast",
    podcast_fav: true,
    published: new Date(Date.now() - 2 * 3600 * 1000).toISOString(),
    duration: 3540,
    position: 1200,
    state: "in_progress",
    uri: "demo://episode/1",
    podcast_uri: null,
  },
  {
    title: "A mesterséges intelligencia és a hosszú út az emberi gondolatig",
    podcast: "Qubit Podcast",
    podcast_fav: true,
    published: new Date(Date.now() - 26 * 3600 * 1000).toISOString(),
    duration: 3180,
    position: 0,
    state: "unplayed",
    uri: "demo://episode/2",
    podcast_uri: null,
  },
  {
    title: "Körkép: így alakult a hét a pénzpiacokon",
    podcast: "Portfolio",
    podcast_fav: false,
    published: new Date(Date.now() - 2 * 86400 * 1000).toISOString(),
    duration: 1740,
    position: 1740,
    state: "finished",
    uri: "demo://episode/3",
    podcast_uri: null,
  },
  {
    title: "Ahol a kék bolygó még kék – óceánkutatás a parttól a mélységig",
    podcast: "Hihetetlen Történelem Podcast",
    podcast_fav: false,
    published: new Date(Date.now() - 4 * 86400 * 1000).toISOString(),
    duration: 4980,
    position: 0,
    state: "unplayed",
    uri: "demo://episode/4",
    podcast_uri: null,
  },
];

class MapodcastsEpisodesCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._imgCache = new Map();
    this._progress = new Map(); // episode uri -> {position, fully_played, state}
    this._lastSignature = null;
    this._raf = null;
    this._pollTimer = null; // setTimeout handle (adaptive reschedule)
    this._burstTimers = new Set(); // post-play burst poll handles
    this._descCache = new Map(); // episode uri -> description (string|null)
    this._descLoading = new Set();
    this._expandedUri = null; // currently open description panel
    this._optimistic = new Map(); // episode uri -> grace deadline (ms epoch)
    this._lastPollAt = 0; // ms epoch of the last successful progress poll
    this._lastPlayed = null; // uri of the episode last started from this card
    this._favOnly = false;
  }

  setConfig(config) {
    if (!config) {
      throw new Error("Invalid configuration");
    }
    this._config = {
      entity: config.entity || null,
      player: config.player || null,
      title: config.title || "Latest episodes",
      max_items: Number(config.max_items) || 20,
      show_played: config.show_played !== false,
    };
    this._lastSignature = null; // force rebuild
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config.entity) {
      const found = Object.keys(hass.states).find((id) => {
        const attrs = hass.states[id]?.attributes;
        return (
          id.startsWith("sensor.") && attrs && Array.isArray(attrs.episodes)
        );
      });
      if (found) this._config.entity = found;
    }
    this._scheduleRender();
  }

  getCardSize() {
    return 6;
  }

  static getStubConfig(hass) {
    const sensor = Object.keys(hass?.states || {}).find((id) => {
      const attrs = hass.states[id]?.attributes;
      return id.startsWith("sensor.") && attrs && Array.isArray(attrs.episodes);
    });
    return {
      entity: sensor || undefined,
      title: "Latest episodes",
      max_items: 20,
    };
  }

  static async getConfigElement() {
    return document.createElement("music-assistant-podcasts-card-editor");
  }

  // ---- data helpers ------------------------------------------------------

  _episodes() {
    const attrs = this._hass?.states?.[this._config.entity]?.attributes;
    let eps =
      attrs && Array.isArray(attrs.episodes) ? attrs.episodes : [];
    if (!this._config.show_played) {
      eps = eps.filter((ep) => this._stateOf(ep) !== "finished");
    }
    if (this._favOnly) {
      eps = eps.filter((ep) => !!ep.podcast_fav);
    }
    if (eps.length) return eps.slice(0, this._config.max_items);
    // no entity configured (e.g. card picker preview) → demo rows
    if (!this._config.entity) {
      return DEMO_EPISODES.slice(0, this._config.max_items);
    }
    return [];
  }

  _stateOf(ep) {
    // while the player is actively playing this episode, the bar is live and
    // the state is in progress (finished-ness comes from fully_played polls)
    if (this._livePlayerPosition(ep) !== null) return "in_progress";
    const dur = ep.duration || 0;
    const live = this._progress.get(ep.uri);
    if (live) {
      const pos = live.position || 0;
      let state;
      if (live.fully_played) {
        // trust "finished" only when the resume point agrees: at the very
        // start (re-listen marker) or near the end. Otherwise the provider
        // probably just hasn't reset the flag yet → still in progress.
        state =
          pos === 0 || (dur > 0 && pos >= dur * 0.9)
            ? "finished"
            : "in_progress";
      } else if (pos > 0) {
        state = "in_progress";
      } else {
        state = "unplayed";
      }
      // grace right after a play click: don't flash back to "finished" while
      // the provider hasn't updated the resume point yet. Polls that arrive
      // after the grace window are trusted again.
      const until = this._optimistic.get(ep.uri);
      if (
        until &&
        state === "finished" &&
        !(dur > 0 && pos >= dur * 0.9) &&
        (Date.now() < until || this._lastPollAt <= until)
      ) {
        state = "in_progress";
      }
      return state;
    }
    // no live data yet: an optimistic play click keeps it in progress
    const until = this._optimistic.get(ep.uri);
    if (until && Date.now() < until) return "in_progress";
    return ep.state || "unplayed";
  }

  // ---- live position from the MA player entity -----------------------------

  _maPlayerId() {
    if (this._config.player) return this._config.player;
    const states = this._hass?.states || {};
    return (
      Object.keys(states).find(
        (id) =>
          id.startsWith("media_player.") &&
          states[id]?.attributes?.device_class === "music_assistant"
      ) || null
    );
  }

  // The resume point stored in MA (what /progress returns) only advances in
  // bigger steps; the HA media_player entity knows the true live position
  // (media_position + media_position_updated_at, interpolated by HA). When
  // the player is currently playing THIS episode, prefer its live position.
  _livePlayerPosition(ep) {
    const id = this._maPlayerId();
    if (!id) return null;
    const st = this._hass?.states?.[id];
    if (!st || st.state !== "playing") return null;
    const attrs = st.attributes || {};
    if (!attrs.media_content_id || attrs.media_content_id !== ep.uri) {
      return null;
    }
    const base = Number(attrs.media_position) || 0;
    const updated = attrs.media_position_updated_at
      ? new Date(attrs.media_position_updated_at).getTime()
      : null;
    let pos =
      base + (updated && !Number.isNaN(updated) ? (Date.now() - updated) / 1000 : 0);
    const dur = Number(attrs.media_duration) || ep.duration || 0;
    if (dur > 0) pos = Math.min(pos, dur);
    return Math.max(0, Math.round(pos));
  }

  _positionOf(ep) {
    const live = this._livePlayerPosition(ep);
    if (live !== null) return live;
    const cached = this._progress.get(ep.uri);
    return cached ? cached.position : ep.position || 0;
  }

  _maUrl() {
    const attrs = this._hass?.states?.[this._config.entity]?.attributes;
    return attrs?.ma_url || "";
  }

  _lang() {
    return (this._hass && this._hass.language) || navigator.language || "en";
  }

  _isHu() {
    return this._lang().toLowerCase().startsWith("hu");
  }

  _relativeDate(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    const diffMs = date.getTime() - Date.now();
    const mins = Math.round(diffMs / 60000);
    const rtf = new Intl.RelativeTimeFormat(this._lang(), { numeric: "auto" });
    if (Math.abs(mins) < 60) return rtf.format(mins, "minute");
    const hours = Math.round(diffMs / 3600000);
    if (Math.abs(hours) < 24) return rtf.format(hours, "hour");
    const days = Math.round(diffMs / 86400000);
    if (Math.abs(days) < 30) return rtf.format(days, "day");
    return date.toLocaleDateString(this._lang(), {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  }

  _durationLabel(sec) {
    if (!sec) return "";
    const mins = Math.max(1, Math.round(sec / 60));
    const unit = this._isHu() ? "perc" : "min";
    return `${mins} ${unit}`;
  }

  _remainingLabel(sec) {
    const mins = Math.max(1, Math.round(sec / 60));
    return this._isHu() ? `még ${mins} perc` : `${mins} min left`;
  }

  _podcastUrl(ep) {
    const maUrl = this._maUrl();
    if (!maUrl || !/^https?:\/\//.test(maUrl) || !ep.podcast_uri) return null;
    // podcast_uri: library://podcast/19  ->  <ma_url>/#/podcasts/library/19
    const [scheme, path] = ep.podcast_uri.split("://");
    if (!path || !path.startsWith("podcast/")) return null;
    const url = `${maUrl}/#/podcasts/${scheme}/${path.slice("podcast/".length)}`;
    // only open links that really point at the configured MA server
    return url.startsWith(maUrl) ? url : null;
  }

  async _loadImage(url, imgEl) {
    if (!url) return;
    if (this._imgCache.has(url)) {
      imgEl.src = this._imgCache.get(url);
      return;
    }
    try {
      if (url.startsWith("/api/")) {
        const res = this._hass.fetchWithAuth
          ? await this._hass.fetchWithAuth(url)
          : await fetch(url, { credentials: "include" });
        if (!res.ok) return;
        const objUrl = URL.createObjectURL(await res.blob());
        this._imgCache.set(url, objUrl);
        imgEl.src = objUrl;
      } else {
        this._imgCache.set(url, url);
        imgEl.src = url;
      }
    } catch {
      // leave the placeholder without an image
    }
  }

  // ---- actions -----------------------------------------------------------

  _play(ep, row) {
    const data = { episode_uri: ep.uri };
    if (this._config.player) data.entity_id = this._config.player;
    this._hass.callService("music_assistant_podcasts", "play_episode", data);
    this._lastPlayed = ep.uri; // this is the one the poll should watch
    // optimistic in-progress + grace against a premature "finished"
    this._optimistic.set(ep.uri, Date.now() + PLAY_GRACE_MS);
    this._showPlayToast(row);
    // burst polls so the progress bar starts moving quickly
    for (const delay of PROGRESS_BURST_DELAYS_MS) {
      const t = setTimeout(() => {
        this._burstTimers.delete(t);
        this._fetchProgress();
      }, delay);
      this._burstTimers.add(t);
    }
  }

  _refresh() {
    this._hass.callService("music_assistant_podcasts", "refresh", {});
  }

  // ---- live progress polling ----------------------------------------------

  _anyInProgress() {
    return this._episodes().some((ep) => this._stateOf(ep) === "in_progress");
  }

  // Only one episode can play at a time: the progress poll targets exactly
  // that one (last started here, else whatever is in progress), so we don't
  // hammer Music Assistant with a connection burst for all visible rows.
  // States of the other rows stay in the _progress cache untouched.
  _activeUri() {
    const eps = this._episodes();
    if (this._lastPlayed && eps.some((ep) => ep.uri === this._lastPlayed)) {
      return this._lastPlayed;
    }
    const inProgress = eps.find((ep) => this._stateOf(ep) === "in_progress");
    return inProgress ? inProgress.uri : null;
  }

  _hasLiveActivity() {
    const now = Date.now();
    for (const until of this._optimistic.values()) {
      if (now < until) return true;
    }
    return this._anyInProgress();
  }

  _startPolling() {
    this._stopPolling();
    this._poll();
  }

  _stopPolling() {
    if (this._pollTimer) {
      clearTimeout(this._pollTimer);
      this._pollTimer = null;
    }
    for (const t of this._burstTimers) clearTimeout(t);
    this._burstTimers.clear();
  }

  _scheduleNextPoll() {
    const seconds = this._hasLiveActivity()
      ? PROGRESS_POLL_ACTIVE_SECONDS
      : PROGRESS_POLL_IDLE_SECONDS;
    this._pollTimer = setTimeout(() => this._poll(), seconds * 1000);
  }

  _poll() {
    this._fetchProgress().finally(() => {
      if (this.isConnected) this._scheduleNextPoll();
    });
  }

  async _fetchProgress() {
    if (!this._hass || !this._config.entity || !this.isConnected) return;
    const active = this._activeUri();
    if (!active) return; // nothing playing — sensor states cover the rest
    try {
      const res = this._hass.fetchWithAuth
        ? await this._hass.fetchWithAuth(
            "/api/music_assistant_podcasts/progress",
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ uris: [active] }),
            }
          )
        : await fetch("/api/music_assistant_podcasts/progress", {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ uris: [active] }),
          });
      if (!res.ok) return; // keep the cached states on transient errors
      const data = await res.json();
      this._lastPollAt = Date.now();
      // merge instead of replacing: rows other than the active one keep
      // their last known state
      this._progress = new Map([...this._progress, ...Object.entries(data || {})]);
      this._applyProgress();
    } catch {
      // ignore — next poll retries
    }
  }

  _applyProgress() {
    // update the rendered rows in place (no rebuild → no scroll jumps)
    const rows = this.shadowRoot.querySelectorAll(".row");
    const eps = this._episodes();
    rows.forEach((row, idx) => {
      const ep = eps[idx];
      if (!ep) return;
      const state = this._stateOf(ep);
      const position = this._positionOf(ep);
      const barWrap = row.querySelector(".progress");
      const bar = row.querySelector(".bar");
      const played = row.querySelector(".played");
      const remaining = row.querySelector(".remaining");
      const remSep = row.querySelector(".rem-sep");
      const showBar = state === "in_progress" && ep.duration > 0;
      if (barWrap) barWrap.style.display = showBar ? "block" : "none";
      if (showBar && bar) {
        bar.style.width = `${Math.min(
          100,
          Math.round((position / ep.duration) * 100)
        )}%`;
      }
      const showRemaining = showBar && ep.duration - position > 0;
      if (remaining) {
        remaining.textContent = showRemaining
          ? this._remainingLabel(ep.duration - position)
          : "";
        remaining.style.display = showRemaining ? "inline" : "none";
      }
      if (remSep) remSep.style.display = showRemaining ? "inline" : "none";
      if (played) played.style.display = state === "finished" ? "inline" : "none";
    });
  }

  // ---- description panel (accordion, one open at a time) -----------------

  _toggleDesc(ep, wrap) {
    if (this._expandedUri === ep.uri) {
      this._closeDesc();
      return;
    }
    this._closeDesc();
    this._openDesc(ep, wrap);
  }

  _closeDesc() {
    const panel = this.shadowRoot.querySelector(".desc-panel");
    if (panel) {
      const wrap = panel.closest(".row-wrap");
      if (wrap) wrap.classList.remove("expanded");
      panel.remove();
    }
    this._expandedUri = null;
  }

  _openDesc(ep, wrap) {
    // the panel is a child of the row wrapper (not of the flex .body), so
    // it always spans the full card width, independent of the actions column
    if (!wrap) return;
    const body = wrap.querySelector(".body");
    if (!body) return;
    this._expandedUri = ep.uri;
    wrap.classList.add("expanded");
    const panel = this._el("div", "desc-panel");
    wrap.appendChild(panel);
    const cached = this._descCache.get(ep.uri);
    if (cached === undefined) {
      this._fetchDesc(ep, panel);
    } else {
      this._fillDesc(ep, panel, cached);
    }
  }

  _fetchDesc(ep, panel) {
    if (this._descLoading.has(ep.uri)) return;
    this._descLoading.add(ep.uri);
    panel.textContent = this._isHu() ? "Betöltés…" : "Loading…";
    const url = "/api/music_assistant_podcasts/description";
    const body = JSON.stringify({ uris: [ep.uri] });
    const req = this._hass.fetchWithAuth
      ? this._hass.fetchWithAuth(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body,
        })
      : fetch(url, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body,
        });
    req
      .then((res) =>
        res.ok ? res.json() : Promise.reject(new Error(String(res.status)))
      )
      .then((data) => {
        const desc = data && ep.uri in data ? data[ep.uri] : null;
        this._descCache.set(ep.uri, desc);
        if (this._expandedUri === ep.uri && panel.isConnected) {
          this._fillDesc(ep, panel, desc);
        }
      })
      .catch(() => {
        if (this._expandedUri !== ep.uri || !panel.isConnected) return;
        panel.textContent = this._isHu()
          ? "Nem sikerült betölteni — kattints az újrakezdéshez."
          : "Failed to load — click to retry.";
        panel.classList.add("desc-error");
        panel.onclick = () => {
          panel.classList.remove("desc-error");
          panel.onclick = null;
          this._fetchDesc(ep, panel);
        };
      })
      .finally(() => this._descLoading.delete(ep.uri));
  }

  _fillDesc(ep, panel, desc) {
    // first line: the episode title (bold), then a blank line, then text
    panel.textContent = "";
    panel.appendChild(this._el("div", "desc-title", ep.title || ""));
    const text =
      this._descriptionText(desc) ||
      (this._isHu()
        ? "Nincs leírás ehhez az epizódhoz."
        : "No description for this episode.");
    panel.appendChild(this._el("div", "desc-body", text));
  }

  _descriptionText(raw) {
    // Provider descriptions often contain HTML. Render them as plain text:
    // strip scripts/styles entirely, keep line structure via block elements.
    if (!raw) return "";
    const doc = new DOMParser().parseFromString(String(raw), "text/html");
    for (const node of doc.querySelectorAll("script, style, iframe, object, embed")) {
      node.remove();
    }
    const lines = [];
    const BLOCK = /^(P|DIV|BR|LI|UL|OL|H[1-6]|BLOCKQUOTE|TR|PRE)$/;
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (child.nodeType === Node.TEXT_NODE) {
          lines.push(child.textContent);
        } else if (child.nodeType === Node.ELEMENT_NODE) {
          if (child.tagName === "BR") {
            lines.push("\n");
            continue;
          }
          if (BLOCK.test(child.tagName) && lines.length) lines.push("\n");
          walk(child);
          if (BLOCK.test(child.tagName)) lines.push("\n");
        }
      }
    };
    walk(doc.body);
    return lines.join("").replace(/\n{3,}/g, "\n\n").trim();
  }

  // ---- play feedback toast -------------------------------------------------

  _showPlayToast(row) {
    if (!row) return;
    const old = row.querySelector(".play-toast");
    if (old) old.remove();
    const toast = this._el(
      "div",
      "play-toast",
      this._isHu() ? "Podcast indítása" : "Starting playback"
    );
    toast.addEventListener("animationend", () => toast.remove());
    row.appendChild(toast);
  }

  // ---- rendering helpers ---------------------------------------------------

  _el(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }

  _haIcon(icon, className) {
    const el = document.createElement("ha-icon");
    el.setAttribute("icon", icon);
    if (className) el.className = className;
    return el;
  }

  _buildMarquee(ep) {
    // Episode title as a hover marquee. A single copy is rendered first;
    // after layout, only when it truly overflows, a second copy (with a
    // middot separator, 10px margins both sides) is appended so the
    // translateX(-50%) loop is seamless and never jumps back to the start.
    const wrap = this._el("div", "title-wrap");
    const inner = this._el("div", "title-inner");
    const text = ep.title || "";
    const makeCopy = () => {
      const s = this._el("span", "title-text", text);
      s.title = text;
      s.addEventListener("click", (ev) => {
        ev.stopPropagation();
        // title click toggles the description panel (not playback)
        this._toggleDesc(ep, ev.target.closest(".row-wrap"));
      });
      return s;
    };
    inner.appendChild(makeCopy());
    wrap.appendChild(inner);
    return { wrap, inner };
  }

  _checkOverflow() {
    // called after layout; upgrades overflowing titles to seamless marquees
    this.shadowRoot.querySelectorAll(".row").forEach((row, idx) => {
      const ep = this._episodes()[idx];
      if (!ep) return;
      const wrap = row.querySelector(".title-wrap");
      const inner = row.querySelector(".title-inner");
      if (!wrap || !inner || inner.dataset.measured) return;
      inner.dataset.measured = "1";
      if (inner.scrollWidth <= wrap.clientWidth + 2) return; // fits, no marquee

      const sep = this._el("span", "title-sep", "·");
      inner.appendChild(sep);
      const s = this._el("span", "title-text", ep.title || "");
      s.title = ep.title || "";
      s.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this._toggleDesc(ep, ev.target.closest(".row-wrap"));
      });
      inner.appendChild(s);
      inner.classList.add("overflowing");
      // ~50 px/s, at least 4 s per loop — a bit faster than before
      inner.style.animationDuration = `${Math.max(
        4,
        Math.round(inner.scrollWidth / 50)
      )}s`;
    });
  }

  _buildRow(ep) {
    // wrapper holds the .row and (when open) the full-width description
    // panel, so the panel width never depends on the actions column
    const rowWrap = this._el("div", "row-wrap");
    const row = this._el("div", "row");

    // left column: cover on top (rows are top-aligned), and in the expanded
    // state a dedicated play button under the cover (the overlay itself is
    // hidden while expanded)
    const imgCol = this._el("div", "img-col");
    const imgWrap = this._el("div", "img-wrap");
    const img = document.createElement("img");
    img.alt = "";
    img.loading = "lazy";
    imgWrap.appendChild(img);
    const overlay = this._el("div", "img-overlay");
    overlay.appendChild(this._haIcon("mdi:play-circle", "overlay-play"));
    overlay.title = "Play";
    overlay.addEventListener("click", () => this._play(ep, row));
    imgWrap.appendChild(overlay);
    imgCol.appendChild(imgWrap);
    const playBtn = document.createElement("button");
    playBtn.type = "button";
    playBtn.className = "play-btn";
    playBtn.title = "Play";
    playBtn.appendChild(this._haIcon("mdi:play-circle", null));
    playBtn.addEventListener("click", () => this._play(ep, row));
    imgCol.appendChild(playBtn);
    row.appendChild(imgCol);
    this._loadImage(ep.image, img);

    const body = this._el("div", "body");

    // row 1 — info strip: podcast name · published · length · remaining
    const meta = this._el("div", "meta");
    const url = this._podcastUrl(ep);
    const name = this._el("span", "podcast-name", ep.podcast || "");
    if (url) {
      name.classList.add("link");
      name.title = ep.podcast || "";
      name.addEventListener("click", (ev) => {
        ev.stopPropagation();
        // new tab via a temporary anchor; the URL is same-origin validated
        // in _podcastUrl (must start with the configured MA server URL)
        const a = document.createElement("a");
        a.href = url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.click();
      });
    }
    meta.appendChild(name);
    meta.appendChild(this._el("span", "meta-sep", "·"));
    meta.appendChild(this._el("span", "date", this._relativeDate(ep.published)));
    const dur = this._durationLabel(ep.duration);
    if (dur) {
      meta.appendChild(this._el("span", "meta-sep", "·"));
      meta.appendChild(this._el("span", "duration", dur));
    }
    const remSep = this._el("span", "meta-sep rem-sep", "·");
    remSep.style.display = "none";
    meta.appendChild(remSep);
    const remaining = this._el("span", "remaining");
    remaining.style.display = "none";
    meta.appendChild(remaining);
    body.appendChild(meta);

    // row 2 — episode title (marquee on hover when overflowing)
    const { wrap } = this._buildMarquee(ep);
    body.appendChild(wrap);

    // progress bar (hidden unless in progress)
    const barWrap = this._el("div", "progress");
    barWrap.style.display = "none";
    barWrap.appendChild(this._el("div", "bar"));
    body.appendChild(barWrap);
    row.appendChild(body);

    // actions: played marker first, then the description chevron
    const actions = this._el("div", "actions");
    const played = this._haIcon("mdi:check-circle", "played");
    played.style.display = "none";
    actions.appendChild(played);
    const chev = this._haIcon("mdi:chevron-down", "chev");
    chev.title = this._isHu() ? "Leírás" : "Description";
    chev.addEventListener("click", (ev) => {
      ev.stopPropagation();
      this._toggleDesc(ep, rowWrap);
    });
    actions.appendChild(chev);
    row.appendChild(actions);

    rowWrap.appendChild(row);
    return rowWrap;
  }

  _buildStyles() {
    return `
      :host {
        display: block;
        /* iOS WebKit text autosizing inflates long nowrap titles — disable it */
        -webkit-text-size-adjust: 100%;
        text-size-adjust: 100%;
      }
      ha-card { padding: 12px 16px; }
      .header {
        display: flex; align-items: center; justify-content: space-between;
        margin-bottom: 6px;
      }
      .header .title {
        font-size: 1.1em; font-weight: 500; display: flex; gap: 8px;
        align-items: center;
      }
      .header .refresh { cursor: pointer; color: var(--secondary-text-color); }
      .header .refresh:hover { color: var(--primary-color, #03a9f4); }
      .header .fav-btn {
        display: flex; align-items: center; gap: 6px;
        margin-inline-start: auto;   /* right-align, keep space from the title */
        margin-inline-end: 8px;      /* spacing before the refresh button */
        padding: 4px 12px; border-radius: 16px;
        border: 1px solid var(--divider-color, #444);
        background: transparent;
        color: var(--secondary-text-color);
        font-size: 0.85em; cursor: pointer;
        transition: color 0.15s ease, border-color 0.15s ease;
      }
      .header .fav-btn:hover { color: var(--primary-color, #03a9f4); }
      .header .fav-btn.active {
        color: var(--warning-color, #ff9800);
        border-color: currentColor;
      }
      .header .fav-btn .fav-star { --mdc-icon-size: 18px; }
      .row-wrap {
        border-top: 1px solid var(--divider-color, #333);
      }
      .row-wrap.expanded .play-btn {
        display: flex; align-items: center; justify-content: center;
      }
      .row {
        position: relative;   /* anchor for the play toast */
        display: flex; align-items: flex-start;   /* cover stays at the top */
        gap: 12px;
        padding: 8px 0;
      }
      .img-col {
        flex: 0 0 48px;
        position: relative;   /* anchor for the floating play button */
      }
      .play-btn {
        /* absolutely positioned so it does NOT grow the row height — the
           description panel below the row then starts right under the text */
        display: none; position: absolute; top: 54px; left: 50%;
        transform: translateX(-50%);
        padding: 0; border: none; background: transparent;
        color: var(--primary-color, #03a9f4); cursor: pointer;
        --mdc-icon-size: 28px;
        transition: color 0.15s ease, transform 0.15s ease;
      }
      .play-btn:hover {
        color: var(--state-active-color, var(--primary-color, #03a9f4));
        transform: translateX(-50%) scale(1.12);
        filter: drop-shadow(0 0 4px var(--primary-color, #03a9f4));
      }
      .play-btn:active { transform: translateX(-50%) scale(0.95); }
      .row.expanded .play-btn {
        display: flex; align-items: center; justify-content: center;
      }
      .play-toast {
        position: absolute; left: 50%; top: 50%;
        transform: translate(-50%, -50%);
        background: var(--primary-color, #03a9f4); color: #fff;
        padding: 6px 14px; border-radius: 16px;
        font-size: 0.85em; white-space: nowrap;
        pointer-events: none; z-index: 5;
        animation: play-toast 1.5s ease forwards;
      }
      @keyframes play-toast {
        0% { opacity: 0; }
        12% { opacity: 1; }
        80% { opacity: 1; }
        100% { opacity: 0; }
      }
      .img-wrap {
        width: 48px; height: 48px;
        position: relative;
      }
      .img-wrap img {
        width: 48px; height: 48px; border-radius: 6px; object-fit: cover;
        background: var(--secondary-background-color, #444);
        display: block;
      }
      .img-overlay {
        position: absolute; inset: 0;
        display: flex; align-items: center; justify-content: center;
        background: rgba(0, 0, 0, 0.45); border-radius: 6px;
        opacity: 0; transition: opacity 0.15s ease; cursor: pointer;
      }
      .row:hover .img-overlay { opacity: 1; }
      /* while the description is open, play lives on the button under the
         cover — the overlay trigger is switched off */
      .row-wrap.expanded .img-overlay { display: none; }
      .img-overlay .overlay-play { color: #fff; --mdc-icon-size: 28px; }
      .body { flex: 1; min-width: 0; }

      /* row 1 — info strip */
      .meta {
        display: flex; align-items: baseline; gap: 6px; font-size: 0.78em;
        color: var(--secondary-text-color);
        white-space: nowrap;
      }
      .podcast-name {
        flex: 0 1 auto; min-width: 0;
        overflow: hidden; text-overflow: ellipsis;
      }
      .podcast-name.link { cursor: pointer; }
      .podcast-name.link:hover {
        text-decoration: underline;
        color: var(--primary-color, #03a9f4);
      }
      .meta-sep, .date, .duration, .remaining { flex-shrink: 0; }

      /* row 2 — episode title marquee */
      .title-wrap { flex: 1; min-width: 0; overflow: hidden; }
      .title-inner { display: inline-flex; white-space: nowrap; }
      .title-text {
        font-size: 0.95em; cursor: pointer;
        color: var(--primary-text-color);
      }
      .title-text:hover { color: var(--primary-color, #03a9f4); }
      .title-sep { margin: 0 10px; flex-shrink: 0; }
      .row:hover .title-inner.overflowing {
        animation: title-marquee 10s linear infinite;
      }
      @keyframes title-marquee {
        from { transform: translateX(0); }
        to { transform: translateX(-50%); }
      }

      .progress {
        height: 3px; border-radius: 2px; margin-top: 5px;
        background: var(--secondary-background-color, #333);
        overflow: hidden;
      }
      .bar {
        height: 100%; width: 0%;
        background: var(--primary-color, #03a9f4);
        transition: width 1s linear;
      }
      .actions {
        display: flex; align-items: center; flex-shrink: 0;
        color: var(--secondary-text-color);
      }
      .actions .chev {
        cursor: pointer;
        transition: transform 0.2s ease;
      }
      .row-wrap.expanded .actions .chev { transform: rotate(180deg); }
      .actions .played { color: var(--success-color, green); }

      /* expandable episode description: a child of .row-wrap so its right
         edge always reaches the card wall; the left margin keeps it aligned
         with the body column (cover 48px + row gap 12px) */
      .desc-panel {
        margin-left: 60px;
        padding: 0 0 12px;
        max-height: ${DESC_PANEL_MAX_HEIGHT}px; overflow-y: auto;
        font-size: 0.95em; line-height: 1.5;
        color: var(--primary-text-color);
        overflow-wrap: anywhere;
        border-radius: 6px;
      }
      .desc-title {
        font-weight: 600;
        margin-bottom: 10px;
      }
      .desc-body { white-space: pre-wrap; }
      .desc-panel.desc-error { cursor: pointer; }
      .empty { color: var(--secondary-text-color); padding: 12px 0; }
    `;
  }

  _scheduleRender() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = null;
      this._render();
    });
  }

  _render() {
    const episodes = this._episodes();
    // volatile play-state fields must not trigger a full rebuild (they change
    // on every play-state update); row-level updates handle those in place
    const stable = episodes.map(({ position: _position, state: _state, ...rest }) => rest);
    const signature = JSON.stringify([stable, this._config, this._favOnly]);
    if (signature === this._lastSignature) {
      this._applyProgress();
      return;
    }
    this._lastSignature = signature;
    this._rebuild(episodes);
  }

  _rebuild(episodes) {
    const root = this.shadowRoot;
    root.textContent = "";

    const style = document.createElement("style");
    style.textContent = this._buildStyles();
    root.appendChild(style);

    const card = document.createElement("ha-card");

    const header = this._el("div", "header");
    const titleWrap = this._el("div", "title");
    titleWrap.appendChild(this._haIcon("mdi:microphone", null));
    titleWrap.appendChild(this._el("span", null, this._config.title));
    header.appendChild(titleWrap);
    const favBtn = document.createElement("button");
    favBtn.className = "fav-btn";
    favBtn.title = this._isHu() ? "Kedvencek" : "Favorites";
    favBtn.appendChild(
      this._haIcon(this._favOnly ? "mdi:star" : "mdi:star-outline", "fav-star")
    );
    favBtn.appendChild(
      this._el("span", null, this._isHu() ? "Kedvencek" : "Favorites")
    );
    favBtn.classList.toggle("active", this._favOnly);
    favBtn.addEventListener("click", () => {
      this._favOnly = !this._favOnly;
      this._lastSignature = null; // force rebuild with the filter applied
      this._render();
    });
    header.appendChild(favBtn);
    const refreshBtn = this._haIcon("mdi:refresh", "refresh");
    refreshBtn.title = "Refresh";
    refreshBtn.addEventListener("click", () => this._refresh());
    header.appendChild(refreshBtn);
    card.appendChild(header);

    if (episodes.length) {
      for (const ep of episodes) {
        const row = this._buildRow(ep);
        card.appendChild(row);
        if (ep.uri === this._expandedUri) {
          // a rebuild (new data) happened while a description was open —
          // re-open it so the panel survives the refresh
          this._openDesc(ep, row);
        }
      }
    } else {
      const loading =
        this._hass && this._config.entity ? "Loading…" : "No episodes.";
      card.appendChild(this._el("div", "empty", loading));
    }

    root.appendChild(card);
    this._applyProgress();
    // measure after layout so the marquee only activates on real overflow
    requestAnimationFrame(() => this._checkOverflow());
  }

  connectedCallback() {
    this._render();
    this._startPolling();
  }

  disconnectedCallback() {
    this._stopPolling();
    this._descLoading.clear();
    if (this._raf) {
      cancelAnimationFrame(this._raf);
      this._raf = null;
    }
    for (const objUrl of this._imgCache.values()) {
      if (objUrl && objUrl.startsWith("blob:")) URL.revokeObjectURL(objUrl);
    }
    this._imgCache.clear();
  }
}

// ---- interactive settings editor ------------------------------------------

class MapodcastsEpisodesEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = null;
    this._hass = null;
    this._built = false;
    this._refs = {};
  }

  setConfig(config) {
    const next = { ...config };
    // HA echoes back the config we just emitted — rebuilding then would wipe
    // the input being edited. Only rebuild when the config really changed.
    if (this._config && this._stable(next) === this._stable(this._config)) {
      return;
    }
    this._config = next;
    if (!this._built) {
      this._build();
      this._built = true;
    }
    this._syncFields();
  }

  set hass(hass) {
    if (this._hass === hass) return;
    this._hass = hass;
    // never rebuild on hass ticks — that resets the inputs being edited
    if (this._refs.entity) this._refs.entity.hass = hass;
    if (this._refs.player) this._refs.player.hass = hass;
  }

  _stable(cfg) {
    // order-independent comparison of the flat config object
    const clean = {};
    for (const key of Object.keys(cfg).sort()) {
      if (cfg[key] !== undefined && cfg[key] !== "") clean[key] = cfg[key];
    }
    return JSON.stringify(clean);
  }

  _emit(newConfig) {
    this._config = newConfig;
    const event = new Event("config-changed", {
      bubbles: true,
      composed: true,
    });
    event.detail = { config: newConfig };
    this.dispatchEvent(event);
  }

  _field(label, input) {
    const wrap = this._el("div", "field");
    const lbl = this._el("label", null, label);
    lbl.htmlFor = `f-${label.replace(/\s+/g, "-").toLowerCase()}`;
    input.id = lbl.htmlFor;
    wrap.appendChild(lbl);
    wrap.appendChild(input);
    return wrap;
  }

  _build() {
    const root = this.shadowRoot;
    root.textContent = "";

    const style = document.createElement("style");
    style.textContent = `
      :host { display: block; }
      .field { display: block; margin-bottom: 12px; }
      label {
        display: block; font-size: 0.85em; margin-bottom: 4px;
        color: var(--secondary-text-color);
      }
      input[type="text"], input[type="number"] {
        width: 100%; box-sizing: border-box; padding: 8px;
        border: 1px solid var(--divider-color, #444); border-radius: 4px;
        background: var(--card-background-color, transparent);
        color: var(--primary-text-color);
      }
      ha-entity-picker { display: block; width: 100%; }
      .check { display: flex; align-items: center; gap: 8px; }
      .check label { margin: 0; }
    `;
    root.appendChild(style);

    const title = this._el("input");
    title.type = "text";
    title.addEventListener("change", () =>
      this._emit({ ...this._config, title: title.value })
    );
    root.appendChild(this._field("Title", title));
    this._refs.title = title;

    const entity = document.createElement("ha-entity-picker");
    entity.allowCustomEntity = true;
    entity.includeDomains = ["sensor"];
    entity.addEventListener("value-changed", (ev) =>
      this._emit({ ...this._config, entity: ev.detail.value || null })
    );
    root.appendChild(this._field("Episodes sensor", entity));
    this._refs.entity = entity;

    const player = document.createElement("ha-entity-picker");
    player.allowCustomEntity = true;
    player.includeDomains = ["media_player"];
    player.addEventListener("value-changed", (ev) =>
      this._emit({ ...this._config, player: ev.detail.value || null })
    );
    root.appendChild(this._field("Player (overrides integration default)", player));
    this._refs.player = player;

    const maxItems = this._el("input");
    maxItems.type = "number";
    maxItems.min = "1";
    maxItems.max = "500";
    maxItems.addEventListener("change", () =>
      this._emit({ ...this._config, max_items: Number(maxItems.value) || 20 })
    );
    root.appendChild(this._field("Max items", maxItems));
    this._refs.maxItems = maxItems;

    const checkWrap = this._el("div", "check");
    const showPlayed = this._el("input");
    showPlayed.type = "checkbox";
    showPlayed.addEventListener("change", () =>
      this._emit({ ...this._config, show_played: showPlayed.checked })
    );
    const lbl = this._el("label", null, "Show played episodes");
    lbl.htmlFor = showPlayed.id = "f-show-played";
    checkWrap.appendChild(showPlayed);
    checkWrap.appendChild(lbl);
    root.appendChild(checkWrap);
    this._refs.showPlayed = showPlayed;
  }

  _syncFields() {
    // update field values only when they differ and the field is not being
    // edited — this is what keeps focus while the user types
    const cfg = this._config || {};
    const setIfIdle = (input, value) => {
      if (!input) return;
      const current = input.type === "checkbox" ? input.checked : input.value;
      const target = input.type === "checkbox" ? !!value : value ?? "";
      if (String(current) === String(target)) return;
      if (this.shadowRoot.activeElement === input) return;
      if (input.type === "checkbox") input.checked = !!value;
      else input.value = value ?? "";
    };
    setIfIdle(this._refs.title, cfg.title ?? "Latest episodes");
    setIfIdle(this._refs.entity, cfg.entity ?? "");
    setIfIdle(this._refs.player, cfg.player ?? "");
    setIfIdle(this._refs.maxItems, cfg.max_items ?? 20);
    setIfIdle(this._refs.showPlayed, cfg.show_played !== false);
  }

  _el(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }
}

customElements.define(CARD_NAME, MapodcastsEpisodesCard);
customElements.define("music-assistant-podcasts-card-editor", MapodcastsEpisodesEditor);

// register in the Lovelace card picker (best effort)
window.customCards = window.customCards || [];
window.customCards.push({
  type: CARD_NAME,
  name: "Music Assistant Podcasts Card",
  description: "Latest podcast episodes from Music Assistant",
  preview: true,
  configurable: true,
});

console.info(`${CARD_NAME} loaded`);
