"""Sensor platform for TIM Hub."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TimHubCoordinator

MAX_ATTR_ENTRIES = 20  # non esporre l'intero storico come attributo, solo i più recenti


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"TIM Hub ({entry.data[CONF_HOST]})",
        manufacturer="TIM / Technicolor",
        model="TIM Hub (Technicolor)",
        configuration_url=f"http://{entry.data[CONF_HOST]}",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: TimHubCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            TimHubWanIpSensor(coordinator, entry),
            TimHubLastCallSensor(coordinator, entry),
            TimHubMissedCallsSensor(coordinator, entry),
        ]
    )


class TimHubWanIpSensor(CoordinatorEntity[TimHubCoordinator], SensorEntity):
    """Public/WAN IP address."""

    _attr_has_entity_name = True
    entity_description = SensorEntityDescription(
        key="wan_ip", name="Indirizzo IP pubblico", icon="mdi:ip-network"
    )

    def __init__(self, coordinator: TimHubCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_wan_ip"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.connection.wan_ip


class TimHubLastCallSensor(CoordinatorEntity[TimHubCoordinator], SensorEntity):
    """Most recent call: state = timestamp, attributes = call details + recent list."""

    _attr_has_entity_name = True
    entity_description = SensorEntityDescription(
        key="last_call", name="Ultima chiamata", icon="mdi:phone-log"
    )

    def __init__(self, coordinator: TimHubCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_last_call"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self):
        entries = self._entries()
        return entries[0].time if entries else None

    @property
    def extra_state_attributes(self):
        entries = self._entries()
        if not entries:
            return {}
        latest = entries[0]
        return {
            "tipo_chiamata": latest.call_type,
            "numero_remoto": latest.remote_number,
            "durata": latest.duration,
            "porta": latest.port,
            "chiamate_recenti": [
                {
                    "orario": e.time,
                    "tipo": e.call_type,
                    "numero": e.remote_number,
                    "durata": e.duration,
                }
                for e in entries[:MAX_ATTR_ENTRIES]
            ],
        }

    def _entries(self):
        if self.coordinator.data is None:
            return []
        return self.coordinator.data.call_log.entries


class TimHubMissedCallsSensor(CoordinatorEntity[TimHubCoordinator], SensorEntity):
    """Count of missed calls found in the (visible) call log."""

    _attr_has_entity_name = True
    entity_description = SensorEntityDescription(
        key="missed_calls", name="Chiamate perse", icon="mdi:phone-missed"
    )

    def __init__(self, coordinator: TimHubCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_missed_calls"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        entries = self.coordinator.data.call_log.entries
        return sum(1 for e in entries if "Missed" in e.call_type)

    @property
    def extra_state_attributes(self):
        if self.coordinator.data is None:
            return {}
        entries = self.coordinator.data.call_log.entries
        missed = [e for e in entries if "Missed" in e.call_type]
        return {
            "chiamate_perse_recenti": [
                {"orario": e.time, "numero": e.remote_number, "porta": e.port}
                for e in missed[:MAX_ATTR_ENTRIES]
            ],
            "statistiche_per_dispositivo": self.coordinator.data.call_log.stats_by_device,
        }
