"""Sensor platform for TIM Hub."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import CallLogEntry, ModemSettings, NetworkDevice
from .const import DOMAIN
from .coordinator import TimHubCoordinator
from .entity import device_info as _device_info

MAX_ATTR_ENTRIES = 20  # non esporre l'intero storico come attributo, solo i più recenti

NO_CALLS_MARKDOWN = "_Nessuna chiamata nel registro._"

# Il modem scrive "unsubscribed" quando l'identificativo del chiamante non è
# attivo sulla linea: mostrarlo così com'è confonde, non è un numero.
NUMBER_UNAVAILABLE = "Numero non disponibile"


def _number(entry: CallLogEntry) -> str:
    return entry.remote_number if entry.has_remote_number else NUMBER_UNAVAILABLE


def _duration(value: str) -> str:
    """The gateway appends a stray 's' to durations (e.g. '00:01:32s')."""
    return value.strip().rstrip("s") or value.strip()


# The gateway logs local time with no timezone, e.g. "2026-07-21 18:21:53".
_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d/%m/%Y %H:%M",
    "%d-%m-%Y %H:%M",
)


def _parse_time(value: str) -> datetime | None:
    """Parse a call timestamp into an aware datetime, or None if unreadable."""
    text = value.strip()
    if not text:
        return None

    parsed = dt_util.parse_datetime(text)
    if parsed is None:
        for time_format in _TIME_FORMATS:
            try:
                parsed = datetime.strptime(text, time_format)
                break
            except ValueError:
                continue

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return parsed


def _cell(value: str) -> str:
    """Make a value safe to drop into a markdown table cell."""
    return value.replace("|", "\\|").strip() or "—"


def _markdown_table(entries: Sequence[CallLogEntry]) -> str:
    """Render calls as a markdown table for use in a Markdown card."""
    if not entries:
        return NO_CALLS_MARKDOWN

    rows = ["| Orario | Tipo | Numero | Durata |", "|---|---|---|---|"]
    rows.extend(
        f"| {_cell(e.time)} | {_cell(e.call_type)} "
        f"| {_cell(e.remote_number) if e.has_remote_number else '_n.d._'} "
        f"| {_cell(_duration(e.duration))} |"
        for e in entries
    )
    return "\n".join(rows)


def _as_dicts(entries: Sequence[CallLogEntry]) -> list[dict]:
    return [
        {
            "orario": e.time,
            "tipo": e.call_type,
            "numero": _number(e),
            "numero_disponibile": e.has_remote_number,
            "esito": e.outcome,
            "durata": _duration(e.duration),
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
                state_source="orario",
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
                state_source="orario",
            ),
            TimHubDevicesSensor(coordinator, entry),
            TimHubFirewallLevelSensor(coordinator, entry),
            TimHubDmzHostSensor(coordinator, entry),
            TimHubDhcpSensor(coordinator, entry),
            TimHubSettingsSensor(coordinator, entry),
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
        return _number(entries[0]) if entries else None

    @property
    def extra_state_attributes(self):
        entries = self._entries()
        if not entries:
            return {"registro_markdown": NO_CALLS_MARKDOWN}

        latest = entries[0]
        recent = entries[:MAX_ATTR_ENTRIES]
        return {
            "orario": latest.time,
            "numero": _number(latest),
            "numero_disponibile": latest.has_remote_number,
            "tipo": latest.kind,
            "esito": latest.outcome,
            "tipo_grezzo": latest.call_type,
            "durata": _duration(latest.duration),
            "porta": latest.port,
            "registro_markdown": _markdown_table(recent),
            "chiamate_recenti": _as_dicts(recent),
            # Diagnostica: se numeri o tipi non tornano, questi tre attributi
            # mostrano esattamente cosa manda il modem e come viene interpretato.
            "tipi_rilevati": sorted({e.call_type for e in entries}),
            "intestazioni_tabella": self.coordinator.data.call_log.headers,
            "righe_grezze": [e.raw_cells for e in entries[:5]],
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
        state_source: str = "numero",
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(key=key, name=name, icon=icon)
        self._kind = kind
        self._state_source = state_source
        # Incoming calls carry no caller ID on this line, so those sensors
        # report *when* the call happened instead of an unusable placeholder.
        if state_source == "orario":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = _device_info(entry)

    def _matching(self) -> list[CallLogEntry]:
        return [e for e in self._entries() if e.kind == self._kind]

    @property
    def native_value(self):
        matching = self._matching()
        if not matching:
            return None
        if self._state_source == "orario":
            return _parse_time(matching[0].time)
        return _number(matching[0])

    @property
    def extra_state_attributes(self):
        matching = self._matching()
        if not matching:
            return {"conteggio": 0, "registro_markdown": NO_CALLS_MARKDOWN}

        latest = matching[0]
        recent = matching[:MAX_ATTR_ENTRIES]
        return {
            "orario": latest.time,
            "numero": _number(latest),
            "numero_grezzo": latest.remote_number,
            "numero_disponibile": latest.has_remote_number,
            "esito": latest.outcome,
            "durata": _duration(latest.duration),
            "porta": latest.port,
            "tipo_grezzo": latest.call_type,
            "conteggio": len(matching),
            "numeri": [_number(e) for e in recent],
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
            "numeri": [_number(e) for e in missed],
            "registro_markdown": _markdown_table(missed),
            "chiamate_perse_recenti": _as_dicts(missed),
            "statistiche_per_dispositivo": self.coordinator.data.call_log.stats_by_device,
        }


# ----------------------------------------------------------------------
# Rete e impostazioni
# ----------------------------------------------------------------------


# Il recorder scarta gli stati con attributi troppo grandi (16 KB), e una
# pagina di configurazione può contenere decine di campi.
MAX_SETTING_FIELDS = 40
MAX_SETTING_VALUE = 100


def _trimmed(fields: dict[str, str]) -> dict[str, str]:
    return {
        key: value[:MAX_SETTING_VALUE]
        for key, value in list(fields.items())[:MAX_SETTING_FIELDS]
    }


def _devices_markdown(devices: Sequence[NetworkDevice]) -> str:
    if not devices:
        return "_Nessun dispositivo rilevato._"

    rows = ["| Dispositivo | IP | MAC | Collegamento |", "|---|---|---|---|"]
    rows.extend(
        f"| {_cell(d.display_name)} | {_cell(d.ip)} | {_cell(d.mac)} "
        f"| {_cell(d.interface)} |"
        for d in devices
    )
    return "\n".join(rows)


def _devices_as_dicts(devices: Sequence[NetworkDevice]) -> list[dict]:
    return [
        {
            "nome": d.display_name,
            "ip": d.ip,
            "mac": d.mac,
            "collegamento": d.interface,
            "connesso": d.connected,
        }
        for d in devices
    ]


class TimHubSettingsSensorBase(CoordinatorEntity[TimHubCoordinator], SensorEntity):
    """Shared access to the settings read from the configuration pages."""

    _attr_has_entity_name = True

    def _settings(self) -> ModemSettings:
        if self.coordinator.data is None:
            return ModemSettings()
        return self.coordinator.data.settings


class TimHubDevicesSensor(CoordinatorEntity[TimHubCoordinator], SensorEntity):
    """How many network interfaces are currently connected, and which."""

    _attr_has_entity_name = True
    entity_description = SensorEntityDescription(
        key="connected_devices", name="Dispositivi connessi", icon="mdi:lan-connect"
    )

    def __init__(self, coordinator: TimHubCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_connected_devices"
        self._attr_device_info = _device_info(entry)

    def _devices(self) -> list[NetworkDevice]:
        if self.coordinator.data is None:
            return []
        return self.coordinator.data.devices

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return sum(1 for d in self._devices() if d.connected)

    @property
    def extra_state_attributes(self):
        devices = self._devices()
        connected = [d for d in devices if d.connected]

        per_interface: dict[str, int] = {}
        for device in connected:
            per_interface[device.interface or "sconosciuto"] = (
                per_interface.get(device.interface or "sconosciuto", 0) + 1
            )

        return {
            "totale_rilevati": len(devices),
            "indirizzi_ip": [d.ip for d in connected if d.ip],
            "indirizzi_mac": [d.mac for d in connected],
            "per_collegamento": per_interface,
            "dispositivi": _devices_as_dicts(connected),
            "dispositivi_non_connessi": _devices_as_dicts(
                [d for d in devices if not d.connected]
            ),
            "elenco_markdown": _devices_markdown(connected),
            # Diagnostica: se nomi o IP non tornano, qui si vede cosa manda il modem.
            "righe_grezze": [d.raw_cells for d in devices[:5]],
        }


class TimHubFirewallLevelSensor(TimHubSettingsSensorBase):
    """Firewall level configured on the modem (Basso / Medio / Alto / ...)."""

    entity_description = SensorEntityDescription(
        key="firewall_level", name="Livello firewall", icon="mdi:security"
    )

    def __init__(self, coordinator: TimHubCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_firewall_level"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self):
        return self._settings().firewall_level

    @property
    def extra_state_attributes(self):
        settings = self._settings()
        return {
            "ping_dall_esterno": settings.ping_response_enabled,
            "accesso_remoto": settings.remote_access_enabled,
            "upnp": settings.upnp_enabled,
        }


class TimHubDmzHostSensor(TimHubSettingsSensorBase):
    """LAN host exposed in the DMZ, if any."""

    entity_description = SensorEntityDescription(
        key="dmz_host", name="Host DMZ", icon="mdi:security-network"
    )

    def __init__(self, coordinator: TimHubCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_dmz_host"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self):
        settings = self._settings()
        if settings.dmz_enabled is False:
            return "Nessuno"
        return settings.dmz_host

    @property
    def extra_state_attributes(self):
        return {"attiva": self._settings().dmz_enabled}


class TimHubDhcpSensor(TimHubSettingsSensorBase):
    """DHCP pool handed out on the LAN."""

    entity_description = SensorEntityDescription(
        key="dhcp_range", name="Intervallo DHCP", icon="mdi:ip-network-outline"
    )

    def __init__(self, coordinator: TimHubCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_dhcp_range"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self):
        return self._settings().dhcp_range

    @property
    def extra_state_attributes(self):
        settings = self._settings()
        return {
            "server_dhcp_attivo": settings.dhcp_enabled,
            "primo_indirizzo": settings.dhcp_start,
            "ultimo_indirizzo": settings.dhcp_end,
            "durata_lease": settings.dhcp_lease_time,
            "ip_del_modem": settings.lan_ip,
            "maschera_di_rete": settings.lan_netmask,
        }


class TimHubSettingsSensor(TimHubSettingsSensorBase):
    """Every setting read from the modem, as attributes.

    The named sensors cover the common cases; this one exposes the rest (and
    makes it visible what a different firmware calls its fields).
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description = SensorEntityDescription(
        key="modem_settings", name="Impostazioni modem", icon="mdi:cog"
    )

    def __init__(self, coordinator: TimHubCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_modem_settings"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return sum(len(fields) for fields in self._settings().raw.values())

    @property
    def extra_state_attributes(self):
        settings = self._settings()
        return {
            "pagine_lette": sorted(settings.raw),
            "pagine_non_lette": settings.errors,
            **{f"campi_{page}": _trimmed(fields) for page, fields in settings.raw.items()},
        }
