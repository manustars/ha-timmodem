"""Async client for the TIM Hub / Technicolor gateway web interface.

Endpoints and behaviour confirmed from a real HAR capture against a TIM
Hub (Technicolor) device:

    GET  /                                  -> HTML page containing a
                                                <meta name="CSRFtoken">
    POST /authenticate  (I, A, CSRFtoken)    -> {"s": "...", "B": "..."}
    POST /authenticate  (M, CSRFtoken)       -> {"M": "..."} or {"error": ...}
    GET  /ajax/internet.lua?auto_update=true -> connection status JSON
    GET  /modals/mmpbx-log-modal.lp          -> Call Log HTML (table)

The router does not use session cookies for the authenticated calls in
our testing; it appears to authorize based on the client's source IP
(only one admin session at a time). We still use a persistent
aiohttp.ClientSession so any cookies the router does set are carried
along automatically.
"""
from __future__ import annotations

import binascii
import logging
import re
from dataclasses import dataclass, field

import aiohttp
from bs4 import BeautifulSoup

from .srp6 import SRPUser

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=10)

_CSRF_RE = re.compile(r'<meta\s+name=["\']CSRFtoken["\']\s+content=["\']([^"\']+)["\']', re.IGNORECASE)


class TimHubError(Exception):
    """Base error."""


class TimHubConnectionError(TimHubError):
    """Router not reachable."""


class TimHubAuthError(TimHubError):
    """Login failed (wrong username/password, or protocol mismatch)."""


@dataclass
class ConnectionStatus:
    wan_ip: str | None = None
    ppp_status: str | None = None
    ppp_state: str | None = None
    connected: bool | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class CallLogEntry:
    time: str
    call_type: str
    local_number: str
    remote_number: str
    duration: str
    port: str


@dataclass
class CallLogResult:
    entries: list[CallLogEntry] = field(default_factory=list)
    stats_by_device: list[dict] = field(default_factory=list)


class TimHubClient:
    """Async client for a TIM Hub / Technicolor gateway."""

    def __init__(self, host: str, port: int, username: str, password: str) -> None:
        self._base = f"http://{host}:{port}"
        self._username = username
        self._password = password
        self._session = aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT)
        self._csrf_token: str | None = None

    async def close(self) -> None:
        await self._session.close()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def _fetch_csrf_token(self) -> str:
        try:
            async with self._session.get(self._base + "/") as resp:
                text = await resp.text()
        except aiohttp.ClientError as err:
            raise TimHubConnectionError(f"Modem non raggiungibile: {err}") from err

        match = _CSRF_RE.search(text)
        if not match:
            raise TimHubAuthError("CSRFtoken non trovato nella pagina di login")
        return match.group(1)

    async def login(self) -> None:
        """Perform the SRP-6 login handshake."""
        token = await self._fetch_csrf_token()
        self._csrf_token = token

        user = SRPUser(self._username, self._password)
        uname, a_bytes = user.start_authentication()

        try:
            async with self._session.post(
                self._base + "/authenticate",
                data={
                    "CSRFtoken": token,
                    "I": uname,
                    "A": binascii.hexlify(a_bytes).decode("ascii"),
                },
                headers={"X-Requested-With": "XMLHttpRequest"},
            ) as resp:
                challenge = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise TimHubConnectionError(f"Errore di rete durante il login: {err}") from err

        if "error" in challenge:
            raise TimHubAuthError(f"Login rifiutato dal modem: {challenge['error']}")

        try:
            bytes_s = binascii.unhexlify(challenge["s"])
            bytes_b = binascii.unhexlify(challenge["B"])
        except (KeyError, binascii.Error) as err:
            raise TimHubAuthError(f"Risposta di login inattesa: {challenge}") from err

        m_bytes = user.process_challenge(bytes_s, bytes_b)
        if m_bytes is None:
            raise TimHubAuthError("Controllo di sicurezza SRP fallito (valore B non valido)")

        async with self._session.post(
            self._base + "/authenticate",
            data={"CSRFtoken": token, "M": binascii.hexlify(m_bytes).decode("ascii")},
            headers={"X-Requested-With": "XMLHttpRequest"},
        ) as resp:
            result = await resp.json(content_type=None)

        if "error" in result:
            raise TimHubAuthError(
                "Utente o password errati (o troppi tentativi falliti di recente)."
            )

        try:
            host_hamk = binascii.unhexlify(result["M"])
        except (KeyError, binascii.Error) as err:
            raise TimHubAuthError(f"Risposta di conferma login inattesa: {result}") from err

        user.verify_session(host_hamk)
        if not user.authenticated():
            raise TimHubAuthError(
                "Verifica finale della sessione fallita: il modem non ha confermato "
                "di conoscere la password (possibile problema di rete/proxy)."
            )

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    async def get_connection_status(self) -> ConnectionStatus:
        async with self._session.get(
            self._base + "/ajax/internet.lua", params={"auto_update": "true"}
        ) as resp:
            data = await resp.json(content_type=None)

        ppp_status = data.get("ppp_status")
        return ConnectionStatus(
            wan_ip=data.get("WAN_IP") or None,
            ppp_status=ppp_status,
            ppp_state=data.get("ppp_state"),
            connected=(ppp_status == "connected") if ppp_status else None,
            raw=data,
        )

    async def get_call_log(self) -> CallLogResult:
        async with self._session.get(self._base + "/modals/mmpbx-log-modal.lp") as resp:
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        result = CallLogResult()

        calllog_table = soup.find("table", id="calllog")
        if calllog_table:
            for row in calllog_table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) != 6:
                    continue
                result.entries.append(
                    CallLogEntry(
                        time=cells[0].get_text(strip=True),
                        call_type=cells[1].get_text(strip=True),
                        local_number=cells[2].get_text(strip=True),
                        remote_number=cells[3].get_text(strip=True),
                        duration=cells[4].get_text(strip=True),
                        port=cells[5].get_text(strip=True),
                    )
                )

        stats_tables = soup.find_all("table", id="stats")
        if stats_tables:
            device_stats_table = stats_tables[0]
            headers = [th.get_text(strip=True) for th in device_stats_table.find_all("th")]
            for row in device_stats_table.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cells) == len(headers):
                    result.stats_by_device.append(dict(zip(headers, cells, strict=True)))

        return result
