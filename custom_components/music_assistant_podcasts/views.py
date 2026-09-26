"""HTTP views for the Music Assistant Podcasts integration."""

from __future__ import annotations

import asyncio
import time

from aiohttp import ClientResponseError, web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    MAAuthError,
    MAConnectionError,
    MusicAssistantPodcastApi,
)
from .const import DOMAIN

# Don't nudge the play-state coordinator more often than this from card polls
# (async_request_refresh is debounced anyway, this just avoids the overhead).
PLAY_NUDGE_MIN_INTERVAL = 10  # seconds


def _first_api(hass: HomeAssistant) -> MusicAssistantPodcastApi | None:
    """Return the api wrapper of the first loaded entry, if any."""
    for data in hass.data.get(DOMAIN, {}).values():
        if isinstance(data, dict) and isinstance(data.get("api"), MusicAssistantPodcastApi):
            return data["api"]
    return None


def _first_play_coordinator(hass: HomeAssistant):
    """Return the PlayStateCoordinator of the first loaded entry, if any."""
    for data in hass.data.get(DOMAIN, {}).values():
        if isinstance(data, dict) and data.get("play_coordinator") is not None:
            return data["play_coordinator"]
    return None


async def _parse_uris(request: web.Request) -> list[str]:
    """Extract and validate the uris list from a JSON POST body."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - malformed JSON must 400, not 500
        raise web.HTTPBadRequest(text="invalid JSON body") from None
    uris = body.get("uris") if isinstance(body, dict) else None
    if not isinstance(uris, list) or not all(isinstance(u, str) for u in uris):
        raise web.HTTPBadRequest(text="'uris' must be a list of strings")
    if len(uris) > 200:
        raise web.HTTPBadRequest(text="too many uris")
    return uris


class MAPodcastImageView(HomeAssistantView):
    """Authenticated proxy for Music Assistant imageproxy images.

    The card can't talk to the MA server directly (no token in the browser),
    so images with an imageproxy id are fetched through this view, which adds
    the stored MA long-lived token server-side.
    """

    url = "/api/music_assistant_podcasts/image"
    name = "api:music_assistant_podcasts:image"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        """Proxy a MA image."""
        hass: HomeAssistant = request.app[KEY_HASS]
        proxy_id = request.query.get("proxy_id")
        if not proxy_id:
            return web.Response(status=400, text="proxy_id is required")

        api = _first_api(hass)
        if api is None:
            return web.Response(status=404, text="No configured integration")

        url = f"{api.api_base_url}/imageproxy/{proxy_id}?size=256&fmt=webp"
        headers = {"Authorization": f"Bearer {api.token}"}

        try:
            async with asyncio.timeout(10):
                async with async_get_clientsession(hass).get(
                    url, headers=headers
                ) as resp:
                    resp.raise_for_status()
                    body = await resp.read()
                    content_type = resp.content_type or "image/webp"
        except TimeoutError:
            return web.Response(status=504, text="Image fetch timed out")
        except ClientResponseError as err:
            return web.Response(status=err.status, text=str(err))
        except OSError:
            return web.Response(status=502, text="Cannot reach Music Assistant")

        return web.Response(
            body=body,
            content_type=content_type,
            headers={"Cache-Control": "max-age=86400"},
        )


class MAPodcastProgressView(HomeAssistantView):
    """Authenticated endpoint returning live play state of episodes.

    The card POSTs the uris of the episodes it currently displays; the
    response maps uri -> {position, fully_played, state} fetched straight
    from MA (authoritative resume points, not the stale sensor snapshot).
    Asked-about uris are also tracked by the PlayStateCoordinator so its
    finish/reset transitions stay visible to the rest of Home Assistant.
    """

    url = "/api/music_assistant_podcasts/progress"
    name = "api:music_assistant_podcasts:progress"
    requires_auth = True

    # throttle for the play-state coordinator nudges (class-level, shared)
    _last_nudge = 0.0

    async def post(self, request: web.Request) -> web.Response:
        """Return {episode_uri: {position, fully_played, state}} for the uris."""
        hass: HomeAssistant = request.app[KEY_HASS]
        api = _first_api(hass)
        if api is None:
            return web.json_response({}, status=404)
        try:
            uris = await _parse_uris(request)
        except web.HTTPBadRequest as err:
            return web.json_response({"error": str(err)}, status=400)

        # keep HA-side state fresh too (debounced by the coordinator)
        play_coord = _first_play_coordinator(hass)
        now = time.monotonic()
        if play_coord is not None and now - self._last_nudge > PLAY_NUDGE_MIN_INTERVAL:
            self._last_nudge = now
            play_coord.track(uris)
            hass.async_create_task(play_coord.async_request_refresh())

        try:
            states = await api.get_states_for_uris(uris)
        except MAAuthError:
            return web.json_response({}, status=401)
        except (MAConnectionError, OSError, TimeoutError) as err:
            # include the reason so endpoint tests / browser debuggers show it
            return web.json_response(
                {"error": f"Music Assistant unreachable: {err}"}, status=502
            )
        return web.json_response(states)


class MAPodcastDescriptionView(HomeAssistantView):
    """Authenticated endpoint returning stored episode descriptions.

    Descriptions can be very long, so they are kept out of the episodes
    sensor: the card asks for a single uri on demand when its description
    panel is opened. Server-side caching (hours) makes repeated openings
    free.
    """

    url = "/api/music_assistant_podcasts/description"
    name = "api:music_assistant_podcasts:description"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Return {episode_uri: description} for the requested uris."""
        hass: HomeAssistant = request.app[KEY_HASS]
        api = _first_api(hass)
        if api is None:
            return web.json_response({}, status=404)
        try:
            uris = await _parse_uris(request)
        except web.HTTPBadRequest as err:
            return web.json_response({"error": str(err)}, status=400)
        try:
            descriptions = await api.get_episode_descriptions(uris)
        except MAAuthError:
            return web.json_response({}, status=401)
        except (MAConnectionError, OSError, TimeoutError):
            return web.json_response({}, status=502)
        return web.json_response(descriptions)
