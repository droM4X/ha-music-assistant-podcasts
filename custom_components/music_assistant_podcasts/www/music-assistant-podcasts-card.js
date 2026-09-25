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
 *   row 2:         episode title (click = play; seamless marquee on hover
 *                  when it does not fit)
 *
 * Extras: hover shows a play overlay on the cover image; podcast name links
 * to the MA web UI; progress bars update live via the progress endpoint.
 */

const CARD_NAME = "music-assistant-podcasts-card";
const PROGRESS_POLL_SECONDS = 30;

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
    this._progress = new Map(); // episode uri -> {position, fully_played}
    this._lastSignature = null;
    this._raf = null;
    this._pollTimer = null;
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
    const live = this._progress.get(ep.uri);
    if (live) {
      if (live.fully_played) return "finished";
      if (live.position > 0) return "in_progress";
      return "unplayed";
    }
    return ep.state || "unplayed";
  }

  _positionOf(ep) {
    const live = this._progress.get(ep.uri);
    return live ? live.position : ep.position || 0;
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

  _play(ep) {
    const data = { episode_uri: ep.uri };
    if (this._config.player) data.entity_id = this._config.player;
    this._hass.callService("music_assistant_podcasts", "play_episode", data);
  }

  _refresh() {
    this._hass.callService("music_assistant_podcasts", "refresh", {});
  }

  // ---- live progress polling ----------------------------------------------

  _startPolling() {
    this._stopPolling();
    this._pollProgress();
    this._pollTimer = setInterval(
      () => this._pollProgress(),
      PROGRESS_POLL_SECONDS * 1000
    );
  }

  _stopPolling() {
    if (this._pollTimer) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
  }

  async _pollProgress() {
    if (!this._hass || !this._config.entity) return;
    try {
      const res = this._hass.fetchWithAuth
        ? await this._hass.fetchWithAuth("/api/music_assistant_podcasts/progress")
        : await fetch("/api/music_assistant_podcasts/progress", {
            credentials: "include",
          });
      if (!res.ok) return;
      const data = await res.json();
      this._progress = new Map(Object.entries(data || {}));
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

  // ---- rendering -----------------------------------------------------------

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
        this._play(ep);
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
        this._play(ep);
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
    const row = this._el("div", "row");

    // cover image with hover play overlay
    const imgWrap = this._el("div", "img-wrap");
    const img = document.createElement("img");
    img.alt = "";
    img.loading = "lazy";
    imgWrap.appendChild(img);
    const overlay = this._el("div", "img-overlay");
    overlay.appendChild(this._haIcon("mdi:play-circle", "overlay-play"));
    overlay.title = "Play";
    overlay.addEventListener("click", () => this._play(ep));
    imgWrap.appendChild(overlay);
    row.appendChild(imgWrap);
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

    // actions: only the played marker
    const actions = this._el("div", "actions");
    const played = this._haIcon("mdi:check-circle", "played");
    played.style.display = "none";
    actions.appendChild(played);
    row.appendChild(actions);

    return row;
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
      .row {
        display: flex; align-items: center; gap: 12px;
        padding: 8px 0; border-top: 1px solid var(--divider-color, #333);
      }
      .img-wrap {
        width: 48px; height: 48px; flex: 0 0 48px;
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
      .actions .played { color: var(--success-color, green); }
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
    const signature = JSON.stringify([episodes, this._config]);
    if (signature !== this._lastSignature) {
      this._lastSignature = signature;
      this._rebuild(episodes);
    } else {
      this._applyProgress();
    }
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
        card.appendChild(this._buildRow(ep));
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
