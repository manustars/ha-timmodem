"""DataUpdateCoordinator for the TIM Hub (Technicolor) integration."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    CallLogResult,
    ConnectionStatus,
    ModemSettings,
    NetworkDevice,
    TimHubAuthError,
    TimHubClient,
    TimHubError,
)

_LOGGER = logging.getLogger(__name__)

# Firewall level, DMZ and DHCP only change when someone edits them, and each
# read costs four extra page loads, so they are not refreshed every cycle.
SETTINGS_INTERVAL = timedelta(minutes=5)


@dataclass
class TimHubData:
    connection: ConnectionStatus
    call_log: CallLogResult
    devices: list[NetworkDevice] = field(default_factory=list)
    settings: ModemSettings = field(default_factory=ModemSettings)


class TimHubCoordinator(DataUpdateCoordinator[TimHubData]):
    """Log in once per cycle and fetch all data."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: TimHubClient,
        update_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="TIM Hub status",
            update_interval=timedelta(seconds=update_interval),
        )
        self.client = client
        self._settings = ModemSettings()
        self._settings_read_at: datetime | None = None

    async def _async_update_data(self) -> TimHubData:
        try:
            await self.client.login()
            connection = await self.client.get_connection_status()
            call_log = await self.client.get_call_log()
        except TimHubAuthError as err:
            # Ritentare ogni 30 secondi con credenziali rifiutate fa scattare il
            # blocco tentativi del modem: meglio fermarsi e chiedere la password.
            raise ConfigEntryAuthFailed(str(err)) from err
        except TimHubError as err:
            raise UpdateFailed(str(err)) from err

        return TimHubData(
            connection=connection,
            call_log=call_log,
            devices=await self._async_devices(),
            settings=await self._async_settings(),
        )

    async def _async_devices(self) -> list[NetworkDevice]:
        """Devices are a bonus: keep the last list rather than fail the update."""
        try:
            return await self.client.get_devices()
        except TimHubError as err:
            _LOGGER.warning("Elenco dispositivi non disponibile: %s", err)
            return self.data.devices if self.data else []

    async def _async_settings(self) -> ModemSettings:
        now = dt_util.utcnow()
        if self._settings_read_at and now - self._settings_read_at < SETTINGS_INTERVAL:
            return self._settings

        try:
            self._settings = await self.client.get_settings()
        except TimHubError as err:
            _LOGGER.warning("Impostazioni del modem non disponibili: %s", err)
        else:
            self._settings_read_at = now
        return self._settings
