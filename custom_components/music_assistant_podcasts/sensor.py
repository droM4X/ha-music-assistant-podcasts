"""Sensor entity for Music Assistant Podcasts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_EPISODES,
    ATTR_FAILED_FEEDS,
    ATTR_FEED_COUNT,
    ATTR_LAST_UPDATED,
    CONF_SERVER_URL,
    DOMAIN,
    MANUFACTURER,
)
from .coordinator import PodcastsCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor from a config entry."""
    coordinator: PodcastsCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    async_add_entities([LatestEpisodesSensor(coordinator, entry)])


class LatestEpisodesSensor(CoordinatorEntity[PodcastsCoordinator], SensorEntity):
    """Sensor listing the newest podcast episodes across all feeds."""

    _attr_has_entity_name = True
    _attr_name = "Latest podcast episodes"
    _attr_icon = "mdi:microphone"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: PodcastsCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_latest_episodes"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Music Assistant Podcasts",
            manufacturer=MANUFACTURER,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=entry.data[CONF_SERVER_URL],
        )
        self._attr_native_value = 0
        self._attr_extra_state_attributes: dict[str, Any] = {}
        self._update_attrs()

    def _update_attrs(self) -> None:
        """Sync attributes (and count) from coordinator data."""
        data = self.coordinator.data or {}
        episodes = data.get(ATTR_EPISODES, [])
        self._attr_native_value = len(episodes)
        self._attr_extra_state_attributes = {
            ATTR_EPISODES: episodes,
            ATTR_FEED_COUNT: len(episodes),
            ATTR_FAILED_FEEDS: data.get(ATTR_FAILED_FEEDS, []),
            ATTR_LAST_UPDATED: datetime.now(tz=timezone.utc).isoformat(),
            "ma_url": data.get("ma_url", ""),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update attributes when new data arrives."""
        self._update_attrs()
        self.async_write_ha_state()
