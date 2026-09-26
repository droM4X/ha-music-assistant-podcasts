"""Thin wrapper around the Music Assistant client library.

Each operation opens a short-lived websocket connection to the MA server.
This is stateless and therefore robust: no reconnect bookkeeping needed,
which is fine for our polling pattern (hourly auto refresh + manual refresh).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from music_assistant_client import MusicAssistantClient, login_with_token
from music_assistant_client.exceptions import (
    CannotConnect,
    MusicAssistantClientException,
)
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import (
    AuthenticationFailed,
    AuthenticationRequired,
    LoginFailed,
)

from .const import (
    DESC_CACHE_TTL,
    EP_DURATION,
    EP_IMAGE,
    EP_PODCAST,
    EP_PODCAST_FAV,
    EP_PODCAST_URI,
    EP_POSITION,
    EP_PUBLISHED,
    EP_STATE,
    EP_TITLE,
    EP_URI,
    MA_AUTH_DISPLAY_NAME,
    STATE_CACHE_TTL,
)

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 15
COMMAND_TIMEOUT = 60
PER_PODCAST_TIMEOUT = 30

STATE_FINISHED = "finished"
STATE_IN_PROGRESS = "in_progress"
STATE_UNPLAYED = "unplayed"

# a "fully played" flag is only trusted when the resume point agrees: at the
# very start (re-listen marker) or near the end of the episode. Otherwise the
# provider probably just hasn't reset the flag yet and the episode is really
# still in progress (this caused "half played shows a green check").
FULLY_PLAYED_TRUST_RATIO = 0.9

CACHE_MAX_ENTRIES = 500

AUTH_ERRORS = (AuthenticationFailed, AuthenticationRequired, LoginFailed)


@dataclass
class EpisodeFetchResult:
    """Result of a refresh cycle."""

    episodes: list[dict[str, Any]] = field(default_factory=list)
    failed_feeds: list[str] = field(default_factory=list)
    feed_count: int = 0


class MAConnectionError(Exception):
    """Cannot reach the Music Assistant server."""


class MAAuthError(Exception):
    """Authentication against the Music Assistant server failed."""


class MusicAssistantPodcastApi:
    """Small helper around the Music Assistant client library."""

    def __init__(self, hass: HomeAssistant, server_url: str, token: str) -> None:
        """Initialize the api wrapper."""
        self.hass = hass
        self.server_url = server_url
        self.token = token
        # uri -> (monotonic_ts, value); short-lived caches for on-demand fetches
        self._desc_cache: dict[str, tuple[float, str | None]] = {}
        self._state_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @property
    def api_base_url(self) -> str:
        """Return the http base URL of the MA server (for imageproxy etc.)."""
        # ws(s):// hosts are served over http(s) by the same MA webserver
        parts = urlparse(self.server_url)
        scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
        return urlunparse(parts._replace(scheme=scheme))

    def _client(self) -> MusicAssistantClient:
        """Create a fresh client instance."""
        return MusicAssistantClient(
            self.server_url,
            async_get_clientsession(self.hass),
            token=self.token,
        )

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[MusicAssistantClient]:
        """Open a short-lived listening-mode connection.

        A background reader task dispatches responses by message_id, which
        makes concurrent send_command calls on this single connection safe.
        Without it, parallel commands would hit aiohttp's "Concurrent call to
        receive() is not allowed".
        """
        client = self._client()
        listen_task = asyncio.create_task(client.start_listening())
        # wait for the client to switch to listening mode. The flag is only
        # set after the full connect (TCP + WS handshake + auth), which needs
        # several network roundtrips — a spin loop with sleep(0) can time out
        # before the handshake finishes on anything but loopback, so use real
        # (small) sleeps under an overall deadline instead.
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + CONNECT_TIMEOUT
            while not getattr(client, "_listening", False):
                if listen_task.done():
                    # connect itself failed — surface the real reason
                    listen_task.result()
                    return
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise MAConnectionError(
                        "Music Assistant client did not enter listening mode"
                    )
                await asyncio.sleep(min(0.05, remaining))
            yield client
        finally:
            listen_task.cancel()
            with contextlib.suppress(
                asyncio.CancelledError, MusicAssistantClientException
            ):
                await listen_task

    # -- connection helpers --------------------------------------------

    async def _get_podcast_episodes(
        self,
        client: MusicAssistantClient,
        podcast: Any,
    ) -> list[dict[str, Any]]:
        """Fetch the full episode list for one podcast (already connected client)."""
        async with asyncio.timeout(PER_PODCAST_TIMEOUT):
            episodes = await client.music.get_podcast_episodes(
                podcast.item_id, podcast.provider
            )
        result: list[dict[str, Any]] = []
        for episode in episodes:
            published = None
            if episode.metadata and episode.metadata.release_date:
                published = episode.metadata.release_date.isoformat()
            result.append(
                {
                    EP_TITLE: episode.name,
                    EP_PODCAST: podcast.name,
                    EP_PODCAST_FAV: bool(getattr(podcast, "favorite", False)),
                    EP_PODCAST_URI: podcast.uri,
                    EP_URI: episode.uri,
                    EP_PUBLISHED: published,
                    EP_DURATION: episode.duration,
                    EP_POSITION: (episode.resume_position_ms or 0) // 1000,
                    EP_STATE: _play_state(
                        episode.fully_played,
                        episode.resume_position_ms,
                        episode.duration,
                    ),
                    EP_IMAGE: _resolve_image(
                        episode.metadata.images if episode.metadata else None,
                        episode.uri,
                    ),
                }
            )
        return result

    async def get_latest_episodes(
        self, episodes_per_feed: int, list_size: int, force_refresh: bool = False
    ) -> EpisodeFetchResult:
        """Return the newest episodes across all library podcasts, sorted by date."""
        async with self._session() as client:
            podcasts = await client.music.get_library_podcasts()
            failed: list[str] = []

            if force_refresh:
                # ask MA to re-read the feeds from the providers first
                async def _refresh_one(podcast: Any) -> None:
                    try:
                        async with asyncio.timeout(PER_PODCAST_TIMEOUT):
                            await client.music.refresh_item(podcast)
                    except (TimeoutError, MusicAssistantClientException, OSError):
                        _LOGGER.warning(
                            "Force refresh failed for podcast %s", podcast.name
                        )

                await asyncio.gather(
                    *[_refresh_one(podcast) for podcast in podcasts]
                )

            async def _fetch_one(podcast: Any) -> list[dict[str, Any]]:
                try:
                    return await self._get_podcast_episodes(client, podcast)
                except AUTH_ERRORS:
                    raise
                except (
                    TimeoutError,
                    MusicAssistantClientException,
                    OSError,
                ) as err:
                    # one broken feed must not break the whole list
                    _LOGGER.warning(
                        "Failed to get episodes for podcast %s: %s",
                        podcast.name,
                        err,
                    )
                    failed.append(podcast.name)
                    return []

            results = await asyncio.gather(
                *[_fetch_one(podcast) for podcast in podcasts]
            )

            # per feed: keep only the newest `episodes_per_feed` episodes, so every
            # feed stays represented in the merged list even when a busy feed
            # would otherwise fill the whole top-N
            candidates: list[dict[str, Any]] = []
            for podcast_eps in results:
                podcast_eps.sort(
                    key=lambda ep: (ep.get(EP_PUBLISHED) or ""), reverse=True
                )
                candidates.extend(podcast_eps[:max(1, episodes_per_feed)])

            episodes = candidates
            # merged list: newest first; missing dates go to the end
            episodes.sort(
                key=lambda ep: (ep.get(EP_PUBLISHED) or ""), reverse=True
            )
            return EpisodeFetchResult(
                episodes=episodes[:list_size],
                failed_feeds=failed,
                feed_count=len(podcasts),
            )

    # -- on-demand episode data -----------------------------------------

    async def _fetch_episode_state(
        self, client: MusicAssistantClient, uri: str
    ) -> dict[str, Any] | None:
        """Fetch authoritative play state for one episode (open connection)."""
        try:
            async with asyncio.timeout(PER_PODCAST_TIMEOUT):
                item = await client.music.get_item_by_uri(uri)
        except (TimeoutError, MusicAssistantClientException, OSError):
            return None
        if item is None or getattr(item, "media_type", None) != MediaType.PODCAST_EPISODE:
            return None
        position_ms = getattr(item, "resume_position_ms", 0) or 0
        fully_played = bool(getattr(item, "fully_played", False))
        duration = getattr(item, "duration", 0) or 0
        return {
            "position": position_ms // 1000,
            "fully_played": fully_played,
            "state": _play_state(fully_played, position_ms, duration),
        }

    async def get_states_for_uris(
        self, uris: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Return authoritative play states for the given episode uris.

        Maps uri -> {position, fully_played, state}. Unlike get_play_states
        this does not depend on MA's in-progress list, so transitions to
        finished (or a resume reset) are seen reliably. Results are cached
        for a few seconds so rapid card polls don't hammer the MA server.
        """
        now = time.monotonic()
        result: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for uri in uris:
            cached = self._state_cache.get(uri)
            if cached and now - cached[0] < STATE_CACHE_TTL:
                result[uri] = cached[1]
            else:
                missing.append(uri)
        if not missing:
            return result
        async with self._session() as client:

            async def _one(uri: str) -> None:
                state = await self._fetch_episode_state(client, uri)
                if state is not None:
                    self._state_cache[uri] = (time.monotonic(), state)
                    result[uri] = state

            await asyncio.gather(*[_one(uri) for uri in missing])
        self._trim_cache(self._state_cache)
        return result

    async def get_play_states(
        self, extra_uris: list[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Return play states for in-progress episodes (plus any extra uris).

        Maps episode uri -> {position: seconds, fully_played: bool, state}.
        Used by the PlayStateCoordinator to keep the sensor attributes (and
        thereby the rest of Home Assistant) in sync without a full refresh.
        """
        async with self._session() as client:
            items = await client.music.in_progress_items(limit=50)
            uris = [
                item.uri
                for item in items
                if item.media_type == MediaType.PODCAST_EPISODE and item.uri
            ]
            for uri in extra_uris or []:
                if uri not in uris:
                    uris.append(uri)

            async def _one(uri: str) -> tuple[str, dict[str, Any]] | None:
                state = await self._fetch_episode_state(client, uri)
                return None if state is None else (uri, state)

            results = await asyncio.gather(*[_one(uri) for uri in uris])

        return {uri: state for entry in results if entry for uri, state in [entry]}

    async def get_episode_descriptions(
        self, uris: list[str]
    ) -> dict[str, str | None]:
        """Return the stored description for the given episode uris.

        Descriptions can be long, so they are deliberately kept out of the
        episodes sensor and fetched on demand (the card asks for one uri
        when its description panel is opened). MA stores them in the item
        metadata, so a single get_item_by_uri per uri is enough. Cached for
        hours — descriptions effectively never change.
        """
        now = time.monotonic()
        result: dict[str, str | None] = {}
        missing: list[str] = []
        for uri in uris:
            cached = self._desc_cache.get(uri)
            if cached and now - cached[0] < DESC_CACHE_TTL:
                result[uri] = cached[1]
            else:
                missing.append(uri)
        if not missing:
            return result
        async with self._session() as client:

            async def _one(uri: str) -> None:
                try:
                    async with asyncio.timeout(PER_PODCAST_TIMEOUT):
                        item = await client.music.get_item_by_uri(uri)
                except (TimeoutError, MusicAssistantClientException, OSError):
                    return
                if item is None or getattr(item, "media_type", None) != MediaType.PODCAST_EPISODE:
                    return
                metadata = getattr(item, "metadata", None)
                description = getattr(metadata, "description", None) if metadata else None
                self._desc_cache[uri] = (time.monotonic(), description)
                result[uri] = description

            await asyncio.gather(*[_one(uri) for uri in missing])
        self._trim_cache(self._desc_cache)
        return result

    @staticmethod
    def _trim_cache(cache: dict) -> None:
        """Bound the cache size (drop everything when it grows too large)."""
        if len(cache) > CACHE_MAX_ENTRIES:
            cache.clear()

    async def validate(self) -> None:
        """Validate the connection and token; raise MAAuthError / MAConnectionError."""
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                async with self._client():
                    pass
        except AUTH_ERRORS as err:
            raise MAAuthError(str(err)) from err
        except (MusicAssistantClientException, OSError, asyncio.TimeoutError) as err:
            raise MAConnectionError(str(err)) from err


def _play_state(
    fully_played: bool | None,
    resume_position_ms: int | None,
    duration: int = 0,
) -> str:
    """Map MA play state to a simple string used by the sensor/card.

    The fully_played flag is only trusted when the resume point agrees:
    either at the very start (a re-listen marker) or near the end of the
    episode. Some providers set the flag early / reset it late, which used
    to mark half-played episodes as finished (green check).
    """
    position_s = (resume_position_ms or 0) // 1000
    if fully_played:
        near_end = duration > 0 and position_s >= duration * FULLY_PLAYED_TRUST_RATIO
        at_start = position_s == 0
        if near_end or at_start:
            return STATE_FINISHED
        return STATE_IN_PROGRESS
    if position_s:
        return STATE_IN_PROGRESS
    return STATE_UNPLAYED


def _resolve_image(images: Any, episode_uri: str | None) -> str | None:
    """Resolve a usable image URL for an episode.

    Preference order:
    - a directly accessible (remote) URL from the provider
    - a MA imageproxy id, served through our authenticated HA proxy view
    """
    if not images:
        return None
    for image in images:
        if getattr(image, "remotely_accessible", False):
            return image.path
    for image in images:
        proxy_id = getattr(image, "proxy_id", None)
        if proxy_id:
            return (
                f"/api/music_assistant_podcasts/image?proxy_id={proxy_id}"
            )
    return None


async def async_login_and_create_token(
    hass: HomeAssistant,
    server_url: str,
    username: str,
    password: str,
) -> str:
    """Login with username/password and create a long-lived MA token."""
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            _user, token = await login_with_token(
                server_url,
                username,
                password,
                token_name=MA_AUTH_DISPLAY_NAME,
                aiohttp_session=async_get_clientsession(hass),
            )
    except LoginFailed as err:
        raise MAAuthError(str(err)) from err
    except (CannotConnect, MusicAssistantClientException, OSError) as err:
        raise MAConnectionError(str(err)) from err
    return token
