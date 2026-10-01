"""Local persistence for saved podcast episodes ("save for later").

Music Assistant knows about *favourite shows* but has no notion of a
favourite/bookmarked episode, so those are kept here instead. Everything
lives in a normal Home Assistant storage collection, which means the data

* survives a restart of Home Assistant,
* is available on every device that talks to this HA instance (the card
  reads it through a sensor, not from localStorage),
* and is part of the regular HA backups.

The store intentionally keeps the *episode row snapshot* exactly in the
shape the card already renders, so a saved episode looks identical to a
freshly fetched one. The playback uri stays the real Music Assistant uri,
so pressing play on a saved episode works exactly like on a listed one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    ATTR_DESCRIPTION,
    ATTR_SAVED_AT,
    DOMAIN,
    EP_DURATION,
    EP_IMAGE,
    EP_PODCAST,
    EP_PODCAST_FAV,
    EP_PODCAST_ITEM_ID,
    EP_PODCAST_URI,
    EP_POSITION,
    EP_PUBLISHED,
    EP_STATE,
    EP_TITLE,
    EP_URI,
    MAX_SAVED_DESCRIPTION_CHARS,
    MAX_SAVED_EPISODES,
    STORAGE_KEY_SAVED,
    STORAGE_VERSION_SAVED,
)

_LOGGER = logging.getLogger(__name__)

# Only these keys are accepted from the card. Everything else in the posted
# episode row is dropped, so a (possibly stale) browser cannot stuff
# arbitrary keys into the storage file.
_EPISODE_FIELDS: tuple[str, ...] = (
    EP_URI,
    EP_TITLE,
    EP_PODCAST,
    EP_PODCAST_URI,
    EP_PODCAST_FAV,
    EP_PODCAST_ITEM_ID,
    EP_PUBLISHED,
    EP_DURATION,
    EP_POSITION,
    EP_STATE,
    EP_IMAGE,
)

# Types accepted for each field, so the sensor never publishes a value the
# card would choke on. Anything else falls back to None (and is dropped for
# the uri, which is the storage key).
_FIELD_TYPES: dict[str, type | tuple[type, ...]] = {
    EP_TITLE: str,
    EP_PODCAST: str,
    EP_PODCAST_URI: str,
    EP_PODCAST_FAV: bool,
    EP_PODCAST_ITEM_ID: (str, int),
    EP_PUBLISHED: str,
    EP_DURATION: int,
    EP_POSITION: int,
    EP_STATE: str,
    EP_IMAGE: str,
}


class SavedEpisodesStore:
    """A tiny async-safe store of saved episode snapshots."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the store (does not read the file yet)."""
        self._store: Store = Store(hass, STORAGE_VERSION_SAVED, STORAGE_KEY_SAVED)
        self._episodes: dict[str, dict[str, Any]] | None = None
        self._listeners: list[Callable[[list[dict[str, Any]]], None]] = []

    # -- loading ---------------------------------------------------------

    async def async_load(self) -> None:
        """Read the storage file once and normalise its content."""
        if self._episodes is not None:
            return
        raw = await self._store.async_load()
        episodes: dict[str, dict[str, Any]] = {}
        if isinstance(raw, dict) and isinstance(raw.get("episodes"), dict):
            for uri, entry in raw["episodes"].items():
                normalised = _normalise_entry(uri, entry)
                if normalised:
                    episodes[uri] = normalised
        self._episodes = episodes
        _LOGGER.debug("Loaded %d saved podcast episode(s)", len(episodes))

    def _data(self) -> dict[str, dict[str, Any]]:
        """Return the (loaded) episode map, loading it on first use."""
        return self._episodes if self._episodes is not None else {}

    # -- reading ---------------------------------------------------------

    def episodes(self) -> list[dict[str, Any]]:
        """Return the saved episodes, newest save first.

        The description is intentionally NOT part of the returned rows — it
        can be long and would bloat the sensor state. It is served on demand
        by the saved_description endpoint, exactly like the descriptions of
        freshly fetched episodes.
        """
        rows = [
            {k: v for k, v in entry.items() if k != ATTR_DESCRIPTION}
            for entry in self._data().values()
        ]
        rows.sort(key=lambda ep: ep.get(ATTR_SAVED_AT) or "", reverse=True)
        return rows

    def descriptions(self, uris: list[str]) -> dict[str, str | None]:
        """Return {uri: description} for the given saved episode uris."""
        data = self._data()
        return {uri: data[uri].get(ATTR_DESCRIPTION) for uri in uris if uri in data}

    def count(self) -> int:
        """Return the number of saved episodes."""
        return len(self._data())

    # -- writing ---------------------------------------------------------

    async def async_save_episode(
        self, episode: dict[str, Any], description: str | None = None
    ) -> bool:
        """Add or replace one saved episode. Returns False when rejected."""
        await self.async_load()
        uri = episode.get(EP_URI)
        if not isinstance(uri, str) or not uri:
            _LOGGER.warning("Refusing to save an episode without a uri")
            return False

        data = self._data()
        if uri not in data and len(data) >= MAX_SAVED_EPISODES:
            raise SavedEpisodesFullError(
                f"Saved episode limit reached ({MAX_SAVED_EPISODES})"
            )

        entry = _normalise_entry(uri, {**episode, EP_URI: uri})
        if entry is None:
            return False

        # keep an already stored description when the caller has none
        if description is None:
            description = data.get(uri, {}).get(ATTR_DESCRIPTION)
        if isinstance(description, str):
            entry[ATTR_DESCRIPTION] = description[:MAX_SAVED_DESCRIPTION_CHARS]

        data[uri] = entry
        await self._async_persist()
        return True

    async def async_remove_episode(self, uri: str) -> bool:
        """Remove one saved episode. Returns False when it was not saved."""
        await self.async_load()
        data = self._data()
        if uri not in data:
            return False
        del data[uri]
        await self._async_persist()
        return True

    async def _async_persist(self) -> None:
        """Write to disk and notify the listeners (e.g. the sensor)."""
        await self._store.async_save({"episodes": self._data()})
        self._notify()

    # -- listeners -------------------------------------------------------

    def add_listener(
        self, listener: Callable[[list[dict[str, Any]]], None]
    ) -> Callable[[], None]:
        """Register a change listener, returns a callable to remove it."""
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def _notify(self) -> None:
        """Call every listener with the current episode list."""
        rows = self.episodes()
        for listener in list(self._listeners):
            try:
                listener(rows)
            except Exception:  # noqa: BLE001 - a broken listener must not break the save
                _LOGGER.exception("Saved episodes listener failed")


class SavedEpisodesFullError(Exception):
    """The saved episode list is at its configured limit."""


def _normalise_entry(uri: object, entry: object) -> dict[str, Any] | None:
    """Coerce one stored/posted entry into the canonical row shape.

    Returns None when the entry has no usable uri — that is the storage key,
    so an entry without one is meaningless. Every other field is optional and
    falls back to None.
    """
    if not isinstance(uri, str) or not uri or not isinstance(entry, dict):
        return None
    row: dict[str, Any] = {EP_URI: uri}
    for field in _EPISODE_FIELDS:
        if field == EP_URI:
            continue
        value = entry.get(field)
        expected = _FIELD_TYPES.get(field)
        if (
            expected is not None
            and value is not None
            and not isinstance(value, expected)
        ):
            _LOGGER.debug(
                "Dropping saved field %s with unexpected type %s",
                field,
                type(value),
            )
            continue
        row[field] = value
    saved_at = entry.get(ATTR_SAVED_AT)
    row[ATTR_SAVED_AT] = (
        saved_at
        if isinstance(saved_at, str) and saved_at
        else datetime.now(tz=timezone.utc).isoformat()
    )
    # descriptions are only carried through when stored, not when posted
    description = entry.get(ATTR_DESCRIPTION)
    if isinstance(description, str):
        row[ATTR_DESCRIPTION] = description[:MAX_SAVED_DESCRIPTION_CHARS]
    return row


def async_get_store(hass: HomeAssistant) -> SavedEpisodesStore:
    """Return the process-wide saved-episodes store, creating it if needed."""
    domain_data: dict = hass.data.setdefault(DOMAIN, {})
    store = domain_data.get("store")
    if store is None:
        store = SavedEpisodesStore(hass)
        domain_data["store"] = store
    return store
