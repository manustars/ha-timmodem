"""Binary sensor platform for TIM Hub."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TimHubCoordinator
from .entity import device_info as _device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: TimHubCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            TimHubConnectivitySensor(coordinator, entry),
            TimHubDmzBinarySensor(coordinator, entry),
        ]
    )


class TimHubConnectivitySensor(CoordinatorEntity[TimHubCoordinator], BinarySensorEntity):
    """True if the modem reports an active internet (PPP) connection."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_has_entity_name = True
    _attr_name = "Connessione Internet"

    def __init__(self, coordinator: TimHubCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_connectivity"
        self._attr_device_info = _device_info(entry)

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.connection.connected

    @property
    def extra_state_attributes(self):
        if self.coordinator.data is None:
            return {}
        conn = self.coordinator.data.connection
        return {
            "wan_ip": conn.wan_ip,
            "ppp_state": conn.ppp_state,
        }


class TimHubDmzBinarySensor(CoordinatorEntity[TimHubCoordinator], BinarySensorEntity):
    """Whether a LAN host is exposed in the DMZ."""

    _attr_has_entity_name = True
    _attr_name = "DMZ attiva"
    _attr_icon = "mdi:security-network"

    def __init__(self, coordinator: TimHubCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_dmz"
        self._attr_device_info = _device_info(entry)

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.settings.dmz_enabled

    @property
    def extra_state_attributes(self):
        if self.coordinator.data is None:
            return {}
        settings = self.coordinator.data.settings
        return {
            "host": settings.dmz_host,
            "livello_firewall": settings.firewall_level,
        }
