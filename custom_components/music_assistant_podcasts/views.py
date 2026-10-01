"""HTTP views for the Music Assistant Podcasts integration."""

from __future__ import annotations

import asyncio
import time

from aiohttp import ClientResponseError, web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from music_assistant_client.exceptions import MusicAssistantClientException

from .api import (
    AUTH_ERRORS,
    MAAuthError,
    MAConnectionError,
    MusicAssistantPodcastApi,
)
from .const import (
    ATTR_DESCRIPTION,
    ATTR_EPISODES,
    DOMAIN,
    EP_PODCAST_FAV,
    EP_PODCAST_URI,
    EP_URI,
)
from .store import SavedEpisodesFullError, async_get_store

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


def _coordinators(hass: HomeAssistant) -> list:
    """Return the PodcastsCoordinator of every loaded entry."""
    return [
        data["coordinator"]
        for data in hass.data.get(DOMAIN, {}).values()
        if isinstance(data, dict) and data.get("coordinator") is not None
    ]


def _patch_podcast_favorite(
    coordinators: list, podcast_uri: str, favorite: bool
) -> None:
    """Flip podcast_fav on the cached episode rows of every coordinator.

    Music Assistant is the source of truth and already got the change, but
    the episode list is only refreshed once an hour — patching it here makes
    the sensor (and therefore the card) reflect the new star immediately
    without paying for a full re-scan of every feed. The next scheduled
    refresh overwrites this with the authoritative value.
    """
    for coordinator in coordinators:
        data = coordinator.data
        if not isinstance(data, dict):
            continue
        episodes = data.get(ATTR_EPISODES)
        if not isinstance(episodes, list):
            continue
        changed = False
        for episode in episodes:
            if (
                isinstance(episode, dict)
                and episode.get(EP_PODCAST_URI) == podcast_uri
                and episode.get(EP_PODCAST_FAV) != favorite
            ):
                episode[EP_PODCAST_FAV] = favorite
                changed = True
        if changed:
            coordinator.async_set_updated_data(data)


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


class MAPodcastFavoriteView(HomeAssistantView):
    """Authenticated endpoint toggling a podcast's Music Assistant favourite.

    Music Assistant does support favourites on library podcasts, so this is a
    thin, authenticated wrapper around the MA command — the card never sees
    the MA token. The favourite flag also flips in the cached episode rows so
    the sensor updates without a full feed refresh.
    """

    url = "/api/music_assistant_podcasts/favorite"
    name = "api:music_assistant_podcasts:favorite"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Set (or clear) the favourite flag of one podcast."""
        hass: HomeAssistant = request.app[KEY_HASS]
        api = _first_api(hass)
        if api is None:
            return web.json_response({"error": "no_configured_integration"}, status=404)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON must 400, not 500
            return web.json_response({"error": "invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid JSON body"}, status=400)

        podcast_uri = body.get("podcast_uri")
        favorite = body.get("favorite")
        if not isinstance(podcast_uri, str) or not podcast_uri:
            return web.json_response({"error": "'podcast_uri' is required"}, status=400)
        if not isinstance(favorite, bool):
            return web.json_response({"error": "'favorite' must be a bool"}, status=400)

        raw_item_id = body.get("podcast_item_id")
        item_id: str | int | None
        if isinstance(raw_item_id, bool) or not isinstance(raw_item_id, (str, int)):
            item_id = None
        else:
            item_id = raw_item_id

        try:
            await api.set_podcast_favorite(podcast_uri, favorite, item_id)
        except AUTH_ERRORS as err:
            return web.json_response({"error": str(err)}, status=401)
        except (MAConnectionError, MusicAssistantClientException, OSError, TimeoutError) as err:
            return web.json_response(
                {"error": f"Music Assistant could not store the favourite: {err}"},
                status=502,
            )

        _patch_podcast_favorite(_coordinators(hass), podcast_uri, favorite)
        return web.json_response({"ok": True, "podcast_uri": podcast_uri, "favorite": favorite})


class MAPodcastSavedEpisodesView(HomeAssistantView):
    """CRUD endpoint for locally saved podcast episodes.

    Music Assistant has no favourite-episode concept, so these rows live in a
    Home Assistant storage collection (see store.py): they survive restarts,
    show up on every device and land in the HA backups.
    """

    url = "/api/music_assistant_podcasts/saved_episodes"
    name = "api:music_assistant_podcasts:saved_episodes"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        """Return the saved episode rows (newest save first)."""
        hass: HomeAssistant = request.app[KEY_HASS]
        store = async_get_store(hass)
        await store.async_load()
        return web.json_response({"episodes": store.episodes()})

    async def post(self, request: web.Request) -> web.Response:
        """Add or replace one saved episode."""
        hass: HomeAssistant = request.app[KEY_HASS]
        store = async_get_store(hass)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON must 400, not 500
            return web.json_response({"error": "invalid JSON body"}, status=400)
        if not isinstance(body, dict) or not isinstance(body.get("episode"), dict):
            return web.json_response({"error": "'episode' object is required"}, status=400)

        episode = body["episode"]
        try:
            await store.async_save_episode(
                episode, description=episode.get(ATTR_DESCRIPTION)
            )
        except SavedEpisodesFullError as err:
            return web.json_response({"error": str(err)}, status=409)
        return web.json_response({"ok": True, "count": store.count()})

    async def delete(self, request: web.Request) -> web.Response:
        """Remove one saved episode and its stored metadata."""
        hass: HomeAssistant = request.app[KEY_HASS]
        store = async_get_store(hass)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON must 400, not 500
            return web.json_response({"error": "invalid JSON body"}, status=400)
        uri = body.get(EP_URI) if isinstance(body, dict) else None
        if not isinstance(uri, str) or not uri:
            return web.json_response({"error": "'uri' is required"}, status=400)

        removed = await store.async_remove_episode(uri)
        return web.json_response({"ok": True, "removed": removed, "count": store.count()})


class MAPodcastSavedDescriptionView(HomeAssistantView):
    """Returns descriptions of saved episodes straight from the local store.

    Mirrors the /description endpoint for freshly fetched episodes: long
    texts stay out of the sensor state and are only handed to the browser
    when a description panel is actually opened.
    """

    url = "/api/music_assistant_podcasts/saved_description"
    name = "api:music_assistant_podcasts:saved_description"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Return {episode_uri: description} for saved uris."""
        hass: HomeAssistant = request.app[KEY_HASS]
        try:
            uris = await _parse_uris(request)
        except web.HTTPBadRequest as err:
            return web.json_response({"error": str(err)}, status=400)
        store = async_get_store(hass)
        await store.async_load()
        return web.json_response(store.descriptions(uris))
