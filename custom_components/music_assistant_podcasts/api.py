"""Thin wrapper around the Music Assistant client library.

Each operation opens a short-lived websocket connection to the MA server.
This is stateless and therefore robust: no reconnect bookkeeping needed,
which is fine for our polling pattern (hourly auto refresh + manual refresh).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
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
)

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 15
COMMAND_TIMEOUT = 60
PER_PODCAST_TIMEOUT = 30

STATE_FINISHED = "finished"
STATE_IN_PROGRESS = "in_progress"
STATE_UNPLAYED = "unplayed"

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
                    EP_STATE: _play_state(episode.fully_played, episode.resume_position_ms),
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
        async with self._client() as client:
            # Move the client into listening mode: a background reader task
            # dispatches responses by message_id, which makes concurrent
            # send_command calls on this single connection safe. Without it,
            # parallel commands would hit aiohttp's "Concurrent call to
            # receive() is not allowed".
            listen_task = asyncio.create_task(client.start_listening())
            # wait until the client switched to listening mode — otherwise our
            # first command would race the background reader on the raw socket
            for _ in range(1000):
                if getattr(client, "_listening", False):
                    break
                await asyncio.sleep(0)
            else:
                raise MAConnectionError(
                    "Music Assistant client did not enter listening mode"
                )
            try:
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
            finally:
                listen_task.cancel()
                with contextlib.suppress(
                    asyncio.CancelledError, MusicAssistantClientException
                ):
                    await listen_task

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

    async def get_play_states(self) -> dict[str, dict[str, Any]]:
        """Return live play state for in-progress podcast episodes.

        Maps episode uri -> {position: seconds, fully_played: bool}.
        Used by the dashboard card to keep the progress bars fresh
        without waiting for the (much less frequent) full refresh.
        """
        async with self._client() as client:
            listen_task = asyncio.create_task(client.start_listening())
            # same listening-mode wait as in get_latest_episodes
            for _ in range(1000):
                if getattr(client, "_listening", False):
                    break
                await asyncio.sleep(0)
            try:
                items = await client.music.in_progress_items(limit=50)
                episodes = [
                    item
                    for item in items
                    if item.media_type == MediaType.PODCAST_EPISODE and item.uri
                ]

                async def _full(item: Any) -> tuple[str, dict[str, Any]] | None:
                    try:
                        async with asyncio.timeout(PER_PODCAST_TIMEOUT):
                            full = await client.music.get_item_by_uri(item.uri)
                    except (TimeoutError, MusicAssistantClientException, OSError):
                        return None
                    if full is None:
                        return None
                    return (
                        item.uri,
                        {
                            "position": (
                                (getattr(full, "resume_position_ms", 0) or 0)
                                // 1000
                            ),
                            "fully_played": bool(
                                getattr(full, "fully_played", False)
                            ),
                        },
                    )

                results = await asyncio.gather(
                    *[_full(item) for item in episodes]
                )
            finally:
                listen_task.cancel()
                with contextlib.suppress(
                    asyncio.CancelledError, MusicAssistantClientException
                ):
                    await listen_task

        return {uri: state for entry in results if entry
                for uri, state in [entry]}

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
    fully_played: bool | None, resume_position_ms: int | None
) -> str:
    """Map MA play state to a simple string used by the sensor/card."""
    if fully_played:
        return STATE_FINISHED
    if resume_position_ms:
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
