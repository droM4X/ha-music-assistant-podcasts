"""HTTP views for the Music Assistant Podcasts integration."""

from __future__ import annotations

import asyncio

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


def _first_api(hass: HomeAssistant) -> MusicAssistantPodcastApi | None:
    """Return the api wrapper of the first loaded entry, if any."""
    for data in hass.data.get(DOMAIN, {}).values():
        if isinstance(data, dict) and isinstance(data.get("api"), MusicAssistantPodcastApi):
            return data["api"]
    return None


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

    Used by the dashboard card to keep progress bars fresh: the card polls
    this every ~30 seconds with its HA-authenticated fetch.
    """

    url = "/api/music_assistant_podcasts/progress"
    name = "api:music_assistant_podcasts:progress"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        """Return {episode_uri: {position, fully_played}} for in-progress items."""
        hass: HomeAssistant = request.app[KEY_HASS]
        api = _first_api(hass)
        if api is None:
            return web.json_response({}, status=404)
        try:
            states = await api.get_play_states()
        except MAAuthError:
            return web.json_response({}, status=401)
        except (MAConnectionError, OSError, TimeoutError):
            return web.json_response({}, status=502)
        return web.json_response(states)
