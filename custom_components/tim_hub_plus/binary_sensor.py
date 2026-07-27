"""Binary sensor platform for TIM Hub."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TimHubCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: TimHubCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TimHubConnectivitySensor(coordinator, entry)])


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"TIM Hub ({entry.data[CONF_HOST]})",
        manufacturer="TIM / Technicolor",
        model="TIM Hub (Technicolor)",
        configuration_url=f"http://{entry.data[CONF_HOST]}",
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
