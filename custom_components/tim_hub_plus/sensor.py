"""Sensor platform for TIM Hub."""
from __future__ import annotations

from collections.abc import Sequence

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import CallLogEntry
from .const import DOMAIN
from .coordinator import TimHubCoordinator

MAX_ATTR_ENTRIES = 20  # non esporre l'intero storico come attributo, solo i più recenti

NO_CALLS_MARKDOWN = "_Nessuna chiamata nel registro._"


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"TIM Hub ({entry.data[CONF_HOST]})",
        manufacturer="TIM / Technicolor",
        model="TIM Hub (Technicolor)",
        configuration_url=f"http://{entry.data[CONF_HOST]}",
    )


def _cell(value: str) -> str:
    """Make a value safe to drop into a markdown table cell."""
    return value.replace("|", "\\|").strip() or "—"


def _markdown_table(entries: Sequence[CallLogEntry]) -> str:
    """Render calls as a markdown table for use in a Markdown card."""
    if not entries:
        return NO_CALLS_MARKDOWN

    rows = ["| Orario | Tipo | Numero | Durata |", "|---|---|---|---|"]
    rows.extend(
        f"| {_cell(e.time)} | {_cell(e.call_type)} | {_cell(e.remote_number)} "
        f"| {_cell(e.duration)} |"
        for e in entries
    )
    return "\n".join(rows)


def _as_dicts(entries: Sequence[CallLogEntry]) -> list[dict]:
    return [
        {
            "orario": e.time,
            "tipo": e.call_type,
            "numero": e.remote_number,
            "durata": e.duration,
            "porta": e.port,
        }
        for e in entries
    ]


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
            TimHubLastCallOfKindSensor(
                coordinator, entry,
                key="last_incoming_call",
                name="Ultima chiamata ricevuta",
                icon="mdi:phone-incoming",
                kind="ricevuta",
            ),
            TimHubLastCallOfKindSensor(
                coordinator, entry,
                key="last_outgoing_call",
                name="Ultima chiamata effettuata",
                icon="mdi:phone-outgoing",
                kind="effettuata",
            ),
            TimHubLastCallOfKindSensor(
                coordinator, entry,
                key="last_missed_call",
                name="Ultima chiamata persa",
                icon="mdi:phone-missed",
                kind="persa",
            ),
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


class TimHubCallSensorBase(CoordinatorEntity[TimHubCoordinator], SensorEntity):
    """Shared access to the call log."""

    _attr_has_entity_name = True

    def _entries(self) -> list[CallLogEntry]:
        if self.coordinator.data is None:
            return []
        return self.coordinator.data.call_log.entries


class TimHubLastCallSensor(TimHubCallSensorBase):
    """Most recent call of any kind: state = number, details in attributes."""

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
        return (entries[0].remote_number or None) if entries else None

    @property
    def extra_state_attributes(self):
        entries = self._entries()
        if not entries:
            return {"registro_markdown": NO_CALLS_MARKDOWN}

        latest = entries[0]
        recent = entries[:MAX_ATTR_ENTRIES]
        return {
            "orario": latest.time,
            "numero": latest.remote_number,
            "tipo": latest.kind,
            "tipo_grezzo": latest.call_type,
            "durata": latest.duration,
            "porta": latest.port,
            "registro_markdown": _markdown_table(recent),
            "chiamate_recenti": _as_dicts(recent),
            # Utile se la classificazione del tipo non dovesse corrispondere:
            # mostra le etichette esatte usate dal modem.
            "tipi_rilevati": sorted({e.call_type for e in entries}),
        }


class TimHubLastCallOfKindSensor(TimHubCallSensorBase):
    """Most recent received / placed / missed call. State = the phone number."""

    def __init__(
        self,
        coordinator: TimHubCoordinator,
        entry: ConfigEntry,
        *,
        key: str,
        name: str,
        icon: str,
        kind: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(key=key, name=name, icon=icon)
        self._kind = kind
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = _device_info(entry)

    def _matching(self) -> list[CallLogEntry]:
        return [e for e in self._entries() if e.kind == self._kind]

    @property
    def native_value(self):
        matching = self._matching()
        return (matching[0].remote_number or None) if matching else None

    @property
    def extra_state_attributes(self):
        matching = self._matching()
        if not matching:
            return {"conteggio": 0, "registro_markdown": NO_CALLS_MARKDOWN}

        latest = matching[0]
        recent = matching[:MAX_ATTR_ENTRIES]
        return {
            "orario": latest.time,
            "numero": latest.remote_number,
            "durata": latest.duration,
            "porta": latest.port,
            "tipo_grezzo": latest.call_type,
            "conteggio": len(matching),
            "registro_markdown": _markdown_table(recent),
            "chiamate_recenti": _as_dicts(recent),
        }


class TimHubMissedCallsSensor(TimHubCallSensorBase):
    """Count of missed calls found in the (visible) call log."""

    entity_description = SensorEntityDescription(
        key="missed_calls", name="Chiamate perse", icon="mdi:phone-missed"
    )

    def __init__(self, coordinator: TimHubCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_missed_calls"
        self._attr_device_info = _device_info(entry)

    def _missed(self) -> list[CallLogEntry]:
        return [e for e in self._entries() if e.kind == "persa"]

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return len(self._missed())

    @property
    def extra_state_attributes(self):
        if self.coordinator.data is None:
            return {}

        missed = self._missed()[:MAX_ATTR_ENTRIES]
        return {
            "numeri": [e.remote_number for e in missed],
            "registro_markdown": _markdown_table(missed),
            "chiamate_perse_recenti": _as_dicts(missed),
            "statistiche_per_dispositivo": self.coordinator.data.call_log.stats_by_device,
        }
