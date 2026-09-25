"""Refresh button entity for Music Assistant Podcasts."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_SERVER_URL, DOMAIN, MANUFACTURER
from .coordinator import PodcastsCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the refresh button from a config entry."""
    coordinator: PodcastsCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    async_add_entities([RefreshButton(coordinator, entry)])


class RefreshButton(CoordinatorEntity[PodcastsCoordinator], ButtonEntity):
    """Button that forces a refresh of the episode list."""

    _attr_has_entity_name = True
    _attr_name = "Refresh episodes"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: PodcastsCoordinator, entry: ConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_refresh"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Music Assistant Podcasts",
            manufacturer=MANUFACTURER,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=entry.data[CONF_SERVER_URL],
        )

    async def async_press(self) -> None:
        """Trigger an (eventually debounced) refresh."""
        await self.coordinator.async_request_refresh()
