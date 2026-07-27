"""DataUpdateCoordinator for the TIM Hub (Technicolor) integration."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CallLogResult, ConnectionStatus, TimHubClient, TimHubError

_LOGGER = logging.getLogger(__name__)


@dataclass
class TimHubData:
    connection: ConnectionStatus
    call_log: CallLogResult


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

    async def _async_update_data(self) -> TimHubData:
        try:
            await self.client.login()
            connection = await self.client.get_connection_status()
            call_log = await self.client.get_call_log()
        except TimHubError as err:
            raise UpdateFailed(str(err)) from err

        return TimHubData(connection=connection, call_log=call_log)
