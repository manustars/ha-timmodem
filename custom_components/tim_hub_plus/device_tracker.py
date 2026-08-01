"""Device tracker platform: one entity per network card seen by the modem."""
from __future__ import annotations

from homeassistant.components.device_tracker import ScannerEntity, SourceType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import NetworkDevice
from .const import DOMAIN
from .coordinator import TimHubCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: TimHubCoordinator = hass.data[DOMAIN][entry.entry_id]
    tracked: set[str] = set()

    @callback
    def _add_new_devices() -> None:
        """Devices come and go; create an entity the first time each is seen."""
        if coordinator.data is None:
            return

        new = []
        for device in coordinator.data.devices:
            if device.mac in tracked:
                continue
            tracked.add(device.mac)
            new.append(TimHubDeviceTracker(coordinator, entry, device.mac))

        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_add_new_devices))
    _add_new_devices()


class TimHubDeviceTracker(CoordinatorEntity[TimHubCoordinator], ScannerEntity):
    """Presence of one MAC address on the modem's network."""

    _attr_has_entity_name = False

    def __init__(
        self, coordinator: TimHubCoordinator, entry: ConfigEntry, mac: str
    ) -> None:
        super().__init__(coordinator)
        self._mac = mac
        self._attr_unique_id = f"{entry.entry_id}_{mac}"
        # Keep the last known details so the entity still describes the device
        # after it drops off the list entirely.
        self._last_seen: NetworkDevice = self._device() or NetworkDevice(mac=mac)

    def _device(self) -> NetworkDevice | None:
        if self.coordinator.data is None:
            return None
        for device in self.coordinator.data.devices:
            if device.mac == self._mac:
                self._last_seen = device
                return device
        return None

    @property
    def source_type(self) -> SourceType:
        return SourceType.ROUTER

    @property
    def name(self) -> str:
        device = self._device() or self._last_seen
        return device.display_name

    @property
    def is_connected(self) -> bool:
        device = self._device()
        return bool(device and device.connected)

    @property
    def mac_address(self) -> str:
        return self._mac

    @property
    def ip_address(self) -> str | None:
        device = self._device() or self._last_seen
        return device.ip or None

    @property
    def hostname(self) -> str | None:
        device = self._device() or self._last_seen
        return device.name or None

    @property
    def icon(self) -> str:
        return "mdi:lan-connect" if self.is_connected else "mdi:lan-disconnect"

    @property
    def extra_state_attributes(self):
        device = self._device() or self._last_seen
        return {
            "collegamento": device.interface,
            "mac": device.mac,
            "ip": device.ip,
        }
