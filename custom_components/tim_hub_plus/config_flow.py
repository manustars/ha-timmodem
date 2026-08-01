"""Config flow for TIM Hub (Technicolor)."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME

from .api import TimHubAuthError, TimHubClient, TimHubConnectionError
from .const import DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default="192.168.0.1"): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_USERNAME, default="admin"): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _async_check_login(credentials: dict[str, Any]) -> str | None:
    """Try the login; return the error key to show, or None on success."""
    client = TimHubClient(
        host=credentials[CONF_HOST],
        port=credentials.get(CONF_PORT, DEFAULT_PORT),
        username=credentials[CONF_USERNAME],
        password=credentials[CONF_PASSWORD],
    )
    try:
        await client.login()
    except TimHubConnectionError as err:
        _LOGGER.error("Connessione al modem fallita: %s", err)
        return "cannot_connect"
    except TimHubAuthError as err:
        _LOGGER.error("Autenticazione fallita: %s", err)
        return "invalid_auth"
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Errore inatteso durante la validazione")
        return "unknown"
    finally:
        await client.close()

    return None


class TimHubConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for TIM Hub."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            error = await _async_check_login(user_input)
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(user_input[CONF_HOST])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"TIM Hub ({user_input[CONF_HOST]})", data=user_input
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Credenziali rifiutate dal modem: richiedile invece di ritentare."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            error = await _async_check_login({**entry.data, **user_input})
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=entry.data[CONF_USERNAME]
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
            description_placeholders={"host": entry.data[CONF_HOST]},
        )
