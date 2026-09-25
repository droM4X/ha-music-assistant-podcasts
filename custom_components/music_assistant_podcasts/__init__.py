"""The Music Assistant Podcasts integration.

Aggregates the newest episodes of all library podcasts into a single
sensor list, with playback services and an auto-registered dashboard card.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.loader import async_get_integration

from . import views
from .api import MusicAssistantPodcastApi
from .const import (
    CARD_FILENAME,
    CARD_STATIC_PATH,
    CONF_SERVER_URL,
    CONF_TOKEN,
    DOMAIN,
)
from .coordinator import PodcastsCoordinator
from .services import async_register_services, async_unregister_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "button"]

STATIC_DIR = Path(__file__).parent / "www"


async def _register_card_frontend(hass: HomeAssistant) -> None:
    """Register the episodes card as a versioned Lovelace resource.

    Mirrors the ha-tilos-player approach: the www dir is served under a
    static path, and the card is added to the Lovelace resource collection
    with the manifest version as cache-busting query. When the manifest
    version changes, the existing resource item is updated in place, so the
    browser refetches the JS. In YAML-mode Lovelace (no resource storage)
    we fall back to the extra module URL mechanism.
    """
    registered: dict = hass.data.setdefault(DOMAIN, {})

    if not registered.get("static_registered"):
        try:
            await hass.http.async_register_static_paths(
                [StaticPathConfig(CARD_STATIC_PATH, str(STATIC_DIR), True)]
            )
            registered["static_registered"] = True
        except (ValueError, HomeAssistantError) as err:
            _LOGGER.warning("Card static path registration skipped: %s", err)

    integration = await async_get_integration(hass, DOMAIN)
    url = f"{CARD_STATIC_PATH}/{CARD_FILENAME}?v={integration.version}"
    resource_path = f"{CARD_STATIC_PATH}/{CARD_FILENAME}"

    lovelace = hass.data["lovelace"]
    resources = (
        lovelace.resources
        if hasattr(lovelace, "resources")
        else lovelace["resources"]
    )
    # force loading of storage resources before inspecting them
    await resources.async_get_info()

    stale_ids: list[str] = []
    found = False
    for item in resources.async_items():
        current = item.get("url", "")
        base = current.split("?")[0]
        if base == resource_path:
            found = True
            # already registered — update the version if it changed
            if current != url:
                if isinstance(resources, ResourceStorageCollection):
                    await resources.async_update_item(item["id"], {"url": url})
                else:
                    add_extra_js_url(hass, url)
        elif base.startswith(CARD_STATIC_PATH):
            # stale card resource from a previous file name — drop it
            if isinstance(resources, ResourceStorageCollection):
                stale_ids.append(item["id"])

    for item_id in stale_ids:
        await resources.async_delete_item(item_id)

    if found:
        return

    # not registered yet
    if isinstance(resources, ResourceStorageCollection):
        await resources.async_create_item(
            {"res_type": "module", "url": url}
        )
    else:
        add_extra_js_url(hass, url)

    # register the authenticated proxy views (image + progress), once
    if not registered.get("view_registered"):
        hass.http.register_view(views.MAPodcastImageView())
        hass.http.register_view(views.MAPodcastProgressView())
        registered["view_registered"] = True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Music Assistant Podcasts from a config entry."""
    # 1) frontend + views + services FIRST — the dashboard card must load
    # even while Music Assistant is unreachable and the entry is retrying.
    await _register_card_frontend(hass)

    async_register_services(hass)

    # 2) connect to Music Assistant and fetch data
    api = MusicAssistantPodcastApi(
        hass,
        entry.data[CONF_SERVER_URL],
        entry.data[CONF_TOKEN],
    )
    coordinator = PodcastsCoordinator(hass, api, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_reload_on_update))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            async_unregister_services(hass)
            hass.data.pop(DOMAIN, None)
    return unload_ok


async def async_reload_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def get_apis(hass: HomeAssistant) -> list[tuple[str, PodcastsCoordinator, object]]:
    """Return (entry_id, coordinator, api) tuples for all loaded entries."""
    return [
        (entry_id, data["coordinator"], data["api"])
        for entry_id, data in hass.data.get(DOMAIN, {}).items()
    ]


def get_coordinator(hass: HomeAssistant, entry_id: str) -> PodcastsCoordinator:
    """Return the coordinator for an entry id (used by services/views)."""
    return hass.data[DOMAIN][entry_id]["coordinator"]
