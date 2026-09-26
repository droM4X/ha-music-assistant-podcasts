"""Data update coordinators for Music Assistant Podcasts."""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from music_assistant_client.exceptions import MusicAssistantClientException

from .api import (
    AUTH_ERRORS,
    STATE_IN_PROGRESS,
    MAAuthError,
    MAConnectionError,
)
from .const import (
    ATTR_FAILED_FEEDS,
    CONF_EPISODES_PER_FEED,
    CONF_LIST_SIZE,
    CONF_UPDATE_INTERVAL_MINUTES,
    DEFAULT_EPISODES_PER_FEED,
    DEFAULT_LIST_SIZE,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    EP_PUBLISHED,
    PLAY_STATE_ACTIVE_INTERVAL,
    PLAY_STATE_IDLE_INTERVAL,
    PLAY_STATE_TRACKED_TTL,
)

_LOGGER = logging.getLogger(__name__)


def _option_int(options: dict, key: str, default: int) -> int:
    """Read an int option defensively."""
    try:
        return int(options.get(key, default))
    except (TypeError, ValueError):
        return default


class PodcastsCoordinator(DataUpdateCoordinator[dict]):
    """Collects the newest episodes from all library podcasts."""

    def __init__(self, hass: HomeAssistant, api, entry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_coordinator",
            update_interval=timedelta(
                minutes=_option_int(
                    entry.options,
                    CONF_UPDATE_INTERVAL_MINUTES,
                    DEFAULT_UPDATE_INTERVAL_MINUTES,
                )
            ),
        )
        self.api = api
        self.entry = entry

    async def _async_update_data(self) -> dict:
        """Fetch the latest episodes from Music Assistant."""
        episodes_per_feed = _option_int(
            self.entry.options, CONF_EPISODES_PER_FEED, DEFAULT_EPISODES_PER_FEED
        )
        list_size = _option_int(
            self.entry.options, CONF_LIST_SIZE, DEFAULT_LIST_SIZE
        )
        force_refresh = bool(
            self.entry.options.get("force_refresh", False)
        )
        try:
            result = await self.api.get_latest_episodes(
                episodes_per_feed, list_size, force_refresh
            )
        except AUTH_ERRORS as err:
            raise ConfigEntryAuthFailed(
                f"Music Assistant authentication failed: {err}"
            ) from err
        except MAAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (MAConnectionError, MusicAssistantClientException, OSError) as err:
            # swallow partial-fetch errors here; the api layer already
            # skips failing feeds individually
            _LOGGER.error("Error fetching podcast episodes: %s", err)
            if self.data is None:
                raise
            return self.data

        episodes = [
            ep for ep in result.episodes if ep.get(EP_PUBLISHED) or ep.get("uri")
        ]
        if (
            result.feed_count
            and len(result.failed_feeds) == result.feed_count
            and not episodes
        ):
            # every feed failed — keep the previous list visible and signal failure
            raise UpdateFailed(
                f"All {result.feed_count} feeds failed to fetch"
            )
        return {
            "episodes": episodes,
            "failed_feeds": result.failed_feeds,
            "feed_count": result.feed_count,
            "ma_url": self.api.api_base_url,
        }

    @property
    def failed_feeds(self) -> list[str]:
        """Return feeds that failed during the last update."""
        if self.data is None:
            return []
        return self.data.get(ATTR_FAILED_FEEDS, [])


class PlayStateCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Lightweight, frequent play-state sync for podcast episodes.

    Kept separate from the (expensive) episode-list refresh: this only asks
    MA for the resume points of the few relevant episodes, so it can afford
    to poll every minute while something is playing. Polls back off to a
    slow interval when nothing is in progress.
    """

    def __init__(self, hass: HomeAssistant, api, entry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_play_state",
            update_interval=timedelta(seconds=PLAY_STATE_IDLE_INTERVAL),
        )
        self.api = api
        # uris the card asked about recently: their finish/reset transitions
        # are watched even after they drop out of MA's in-progress list
        self._tracked: dict[str, float] = {}

    def track(self, uris: list[str]) -> None:
        """Mark uris as interesting for the next update cycles."""
        now = time.monotonic()
        for uri in uris:
            if uri:
                self._tracked[uri] = now

    async def _async_update_data(self) -> dict[str, dict]:
        """Fetch authoritative play states for in-progress + tracked episodes."""
        now = time.monotonic()
        cutoff = now - PLAY_STATE_TRACKED_TTL
        extra_uris = [uri for uri, ts in self._tracked.items() if ts >= cutoff]
        self._tracked = {
            uri: ts for uri, ts in self._tracked.items() if ts >= cutoff
        }
        try:
            states = await self.api.get_play_states(extra_uris=extra_uris)
        except AUTH_ERRORS as err:
            raise ConfigEntryAuthFailed(
                f"Music Assistant authentication failed: {err}"
            ) from err
        except MAAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (MAConnectionError, MusicAssistantClientException, OSError) as err:
            # keep the last known states visible; retry on the next cycle
            _LOGGER.warning("Error fetching play states: %s", err)
            if self.data is None:
                raise UpdateFailed(str(err)) from err
            return self.data

        # adaptive cadence: fast while something is playing, slow otherwise
        active = any(
            state.get("state") == STATE_IN_PROGRESS for state in states.values()
        )
        self.update_interval = timedelta(
            seconds=PLAY_STATE_ACTIVE_INTERVAL if active else PLAY_STATE_IDLE_INTERVAL
        )
        return states
