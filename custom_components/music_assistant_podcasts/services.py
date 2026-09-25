"""Services for Music Assistant Podcasts."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import CONF_DEFAULT_PLAYER, DOMAIN, SERVICE_PLAY_EPISODE, SERVICE_REFRESH

_LOGGER = logging.getLogger(__name__)

ATTR_EPISODE_URI = "episode_uri"

REFRESH_SCHEMA = vol.Schema({})

PLAY_EPISODE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_EPISODE_URI): cv.string,
        vol.Optional(ATTR_ENTITY_ID): cv.string,
    }
)


@callback
def _coordinators(hass: HomeAssistant) -> list:
    """Return all loaded coordinators."""
    return [
        data["coordinator"]
        for data in hass.data.get(DOMAIN, {}).values()
        if isinstance(data, dict) and "coordinator" in data
    ]


def _default_player(hass: HomeAssistant) -> str | None:
    """Return the default player configured in any entry options."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        player = entry.options.get(CONF_DEFAULT_PLAYER)
        if player:
            return str(player)
    return None


def _resolve_player(hass: HomeAssistant, entity_id: str | None) -> str:
    """Resolve the target media player entity."""
    if entity_id:
        return entity_id
    player = _default_player(hass)
    if player:
        return player
    # last resort: first media_player state that belongs to music_assistant
    for state in hass.states.async_all("media_player"):
        if state.attributes.get("device_class") == "music_assistant":
            return state.entity_id
    raise HomeAssistantError("No media player found to play the episode on")


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register integration services (idempotent)."""

    async def _handle_refresh(call: ServiceCall) -> None:
        """Force a refresh of all episode lists."""
        for coordinator in _coordinators(hass):
            await coordinator.async_request_refresh()

    async def _handle_play_episode(call: ServiceCall) -> None:
        """Play a single podcast episode on a Music Assistant player."""
        episode_uri = call.data[ATTR_EPISODE_URI]
        entity_id = _resolve_player(hass, call.data.get(ATTR_ENTITY_ID))
        await hass.services.async_call(
            "media_player",
            "play_media",
            {
                ATTR_ENTITY_ID: entity_id,
                "media_content_type": "podcast_episode",
                "media_content_id": episode_uri,
            },
            blocking=True,
        )

    if hass.services.has_service(DOMAIN, SERVICE_REFRESH):
        return
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH, _handle_refresh, schema=REFRESH_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PLAY_EPISODE,
        _handle_play_episode,
        schema=PLAY_EPISODE_SCHEMA,
    )


@callback
def async_unregister_services(hass: HomeAssistant) -> None:
    """Unregister services when the last entry is unloaded."""
    hass.services.async_remove(DOMAIN, SERVICE_REFRESH)
    hass.services.async_remove(DOMAIN, SERVICE_PLAY_EPISODE)
