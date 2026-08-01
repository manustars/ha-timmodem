"""Shared device registry entry for every TIM Hub entity."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


def device_info(entry: ConfigEntry) -> DeviceInfo:
    """The modem itself: all entities of the entry hang off this device."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"TIM Hub ({entry.data[CONF_HOST]})",
        manufacturer="TIM / Technicolor",
        model="TIM Hub (Technicolor)",
        configuration_url=f"http://{entry.data[CONF_HOST]}",
    )
