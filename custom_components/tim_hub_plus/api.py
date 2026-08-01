"""Async client for the TIM Hub / Technicolor gateway web interface.

Endpoints and behaviour confirmed from a real HAR capture against a TIM
Hub (Technicolor) device:

    GET  /                                  -> HTML page containing a
                                                <meta name="CSRFtoken">
    POST /authenticate  (I, A, CSRFtoken)    -> {"s": "...", "B": "..."}
    POST /authenticate  (M, CSRFtoken)       -> {"M": "..."} or {"error": ...}
    GET  /ajax/internet.lua?auto_update=true -> connection status JSON
    GET  /modals/mmpbx-log-modal.lp          -> Call Log HTML (table)
    GET  /modals/device-modal.lp             -> connected devices HTML (table)
    GET  /modals/{firewall,wanservices,ethernet,internet}-modal.lp
                                             -> settings HTML (forms)

The set of ``/modals/*.lp`` pages that exist on this firmware was probed
directly against the device (missing pages answer 404), which is why e.g.
``nat-modal.lp`` and ``gateway-modal.lp`` are not queried here.

The gateway *does* rely on a session cookie set by ``GET /``: without it
nginx answers 403 to /authenticate. Because the router is addressed by
IP, the session must use ``CookieJar(unsafe=True)`` — aiohttp's default
jar silently drops cookies from IP hosts ("Don't accept cookies from
IPs"), which leaves every authenticated request unauthenticated.
"""
from __future__ import annotations

import binascii
import json
import logging
import re
from dataclasses import dataclass, field

import aiohttp
from bs4 import BeautifulSoup

from .srp6 import SRPUser

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=10)

_CSRF_NAME_RE = re.compile(r"^\s*CSRFtoken\s*$", re.IGNORECASE)

# Last-resort fallback: token embedded in inline JavaScript rather than markup.
_CSRF_JS_RE = re.compile(
    r'CSRFtoken["\']?\s*[:=]\s*["\']([A-Za-z0-9+/=_-]{8,})["\']', re.IGNORECASE
)


def _extract_csrf_token(html: str) -> str | None:
    """Find the CSRF token regardless of attribute order or quoting style."""
    soup = BeautifulSoup(html, "html.parser")

    meta = soup.find("meta", attrs={"name": _CSRF_NAME_RE})
    if meta and meta.get("content"):
        return meta["content"].strip()

    field = soup.find("input", attrs={"name": _CSRF_NAME_RE})
    if field and field.get("value"):
        return field["value"].strip()

    match = _CSRF_JS_RE.search(html)
    if match:
        return match.group(1)

    return None


def _describe_auth_error(error) -> str:
    """Render the gateway's error payload.

    With the "login failure" feature on (``loginFailureAttempt`` in the login
    page) the gateway answers with a counter and a lockout timer instead of a
    plain message.
    """
    if isinstance(error, dict):
        wait_time = error.get("waitTime")
        wrong_count = error.get("wrongCount")
        parts = []
        if wrong_count:
            parts.append(f"{wrong_count} tentativi errati registrati")
        if wait_time:
            parts.append(f"il modem è bloccato per altri {wait_time} secondi")
        if parts:
            return "; ".join(parts)
    return str(error)


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


# The gateway labels call types in its own wording (and language), so match
# loosely rather than against one exact string.
_MISSED_LABELS = ("missed", "pers")
_INCOMING_LABELS = ("received", "ricevut", "incoming", "entrant", "answered")
_OUTGOING_LABELS = ("dialed", "outgoing", "effettuat", "uscent", "placed")

# What the gateway writes in the "Remote Number" column when caller ID is not
# available — e.g. "unsubscribed" when the CLI service is not active on the
# line. These are placeholders, not phone numbers.
_PLACEHOLDER_NUMBERS = frozenset(
    {
        "",
        "-",
        "n/a",
        "na",
        "unsubscribed",
        "unknown",
        "unavailable",
        "private",
        "anonymous",
        "restricted",
        "withheld",
        "sconosciuto",
        "anonimo",
        "riservato",
    }
)


@dataclass
class CallLogEntry:
    time: str
    call_type: str
    local_number: str
    remote_number: str
    duration: str
    port: str
    raw_cells: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        """Normalised call type: persa / ricevuta / effettuata / sconosciuta."""
        raw = self.call_type.strip().lower()
        if any(label in raw for label in _MISSED_LABELS):
            return "persa"
        if any(label in raw for label in _INCOMING_LABELS):
            return "ricevuta"
        if any(label in raw for label in _OUTGOING_LABELS):
            return "effettuata"
        return "sconosciuta"

    @property
    def has_remote_number(self) -> bool:
        """False when the gateway had no caller ID to record."""
        return self.remote_number.strip().lower() not in _PLACEHOLDER_NUMBERS

    @property
    def outcome(self) -> str:
        """Call result: riuscita / fallita / persa / sconosciuto."""
        raw = self.call_type.strip().lower()
        if any(label in raw for label in _MISSED_LABELS):
            return "persa"
        if "fail" in raw or "fallit" in raw:
            return "fallita"
        if "success" in raw or "riuscit" in raw:
            return "riuscita"
        return "sconosciuto"


@dataclass
class CallLogResult:
    entries: list[CallLogEntry] = field(default_factory=list)
    stats_by_device: list[dict] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)


# Column headers differ between firmware versions (and languages), so locate
# each field by its heading instead of assuming a fixed column order.
_COLUMN_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("remote_number", ("remote", "remoto", "caller", "chiamante", "corrispondente")),
    ("local_number", ("local", "locale", "linea", "line")),
    ("time", ("time", "orario", "ora", "data", "date")),
    ("call_type", ("type", "tipo")),
    ("duration", ("durat", "length")),
    ("port", ("port", "servi", "device", "dispositiv")),
)

_FALLBACK_ORDER = ("time", "call_type", "local_number", "remote_number", "duration", "port")


def _map_columns(headers: list[str]) -> dict[str, int]:
    """Map field names to column indices using the table headings."""
    mapping: dict[str, int] = {}
    for index, header in enumerate(headers):
        text = header.strip().lower()
        for field_name, keywords in _COLUMN_RULES:
            if field_name not in mapping and any(k in text for k in keywords):
                mapping[field_name] = index
                break

    # A single generic "Number" column means the other party's number.
    if "remote_number" not in mapping:
        taken = set(mapping.values())
        for index, header in enumerate(headers):
            if index not in taken and any(
                k in header.strip().lower() for k in ("number", "numero")
            ):
                mapping["remote_number"] = index
                break

    return mapping


def _entry_from_cells(cells: list[str], mapping: dict[str, int]) -> CallLogEntry | None:
    """Build an entry from one row, by header mapping or by position."""
    if mapping and max(mapping.values()) < len(cells):
        values = {name: cells[index] for name, index in mapping.items()}
    elif len(cells) == len(_FALLBACK_ORDER):
        values = dict(zip(_FALLBACK_ORDER, cells, strict=True))
    else:
        return None

    return CallLogEntry(
        time=values.get("time", ""),
        call_type=values.get("call_type", ""),
        local_number=values.get("local_number", ""),
        remote_number=values.get("remote_number", ""),
        duration=values.get("duration", ""),
        port=values.get("port", ""),
        raw_cells=cells,
    )


# ----------------------------------------------------------------------
# Connected devices
# ----------------------------------------------------------------------

DEVICES_PATH = "/modals/device-modal.lp"

_MAC_RE = re.compile(r"\b([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\b")
_IPV4_RE = re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3})\b")

_CONNECTED_LABELS = ("connected", "connesso", "connessa", "online", "active", "attiv", "up")
_DISCONNECTED_LABELS = (
    "disconnected", "non connesso", "offline", "inactive", "inattiv", "down"
)

# Identifies the "how is it attached" column rather than a name. Matched on
# whole words: host names such as "Milano-PC" contain "lan" by accident.
_INTERFACE_RE = re.compile(
    r"\b(eth(ernet)?\d*|wifi|wi-fi|wireless|wlan\d*|lan\d*|ssid|usb|"
    r"cablat\w*|senza\s+fili|\d(\.\d)?\s*ghz)\b",
    re.IGNORECASE,
)

_DEVICE_STATE_WORDS = frozenset(
    {"connected", "disconnected", "connesso", "non connesso", "online", "offline",
     "active", "inactive", "attivo", "inattivo", "yes", "no", "si", "sì", "-", ""}
)


@dataclass
class NetworkDevice:
    """One network interface (card) known to the gateway."""

    mac: str
    name: str = ""
    ip: str = ""
    interface: str = ""
    connected: bool = True
    raw_cells: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.name or self.ip or self.mac


def _looks_like_login_page(html: str) -> bool:
    """The gateway serves the login page (HTTP 200) when the session expired.

    Matched on the SRP form field ids, which appear only on that page.
    """
    lowered = html.lower()
    return "srp_username" in lowered or "srp_password" in lowered


def _is_state_cell(text: str) -> bool:
    return text.strip().lower() in _DEVICE_STATE_WORDS


def _row_connected(cells: list[str]) -> bool:
    """Read the state column; assume connected when the table has none."""
    for cell in cells:
        text = cell.strip().lower()
        if any(label in text for label in _DISCONNECTED_LABELS):
            return False
        if any(label in text for label in _CONNECTED_LABELS):
            return True
    return True


def _parse_devices(html: str) -> list[NetworkDevice]:
    """Pull every row that carries a MAC address, whatever the table layout.

    Firmware versions differ in column order and headings, so rows are
    recognised by content (a MAC address) instead of by position.
    """
    soup = BeautifulSoup(html, "html.parser")
    devices: list[NetworkDevice] = []
    seen: set[str] = set()

    for row in soup.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
        # The friendly name is editable, so it lives in an <input value="...">
        # rather than in the row's text — and it beats any text column as a name.
        typed_values = [
            value.strip()
            for value in (inp.get("value") for inp in row.find_all("input"))
            if value
            and value.strip()
            and not _MAC_RE.search(value)
            and not _IPV4_RE.search(value)
        ]
        cells += typed_values

        mac_match = _MAC_RE.search(" ".join(cells))
        if not mac_match:
            continue

        mac = mac_match.group(1).lower()
        if mac in seen:
            continue
        seen.add(mac)

        ip = ""
        name = typed_values[0] if typed_values else ""
        interface = ""
        for cell in cells:
            text = cell.strip()
            if not text or _MAC_RE.search(text):
                continue
            ip_match = _IPV4_RE.search(text)
            # 255.x is a netmask, not an address handed out to the device.
            if ip_match and not ip and not ip_match.group(1).startswith("255."):
                ip = ip_match.group(1)
                continue
            if not interface and _INTERFACE_RE.search(text):
                interface = text
                continue
            if not name and not _is_state_cell(text):
                name = text

        devices.append(
            NetworkDevice(
                mac=mac,
                name=name,
                ip=ip,
                interface=interface,
                connected=_row_connected(cells),
                raw_cells=cells,
            )
        )

    return devices


# ----------------------------------------------------------------------
# Modem settings (firewall level, DMZ, DHCP, ...)
# ----------------------------------------------------------------------

# Every page here exists on the tested firmware; a field is looked up across
# all of them, so it does not matter which page a given setting lives on.
SETTINGS_PATHS: dict[str, str] = {
    "firewall": "/modals/firewall-modal.lp",
    "wanservices": "/modals/wanservices-modal.lp",
    "ethernet": "/modals/ethernet-modal.lp",
    "internet": "/modals/internet-modal.lp",
}

_TRUE_VALUES = frozenset(
    {"1", "on", "true", "yes", "enabled", "enable", "attivo", "attiva", "abilitato",
     "abilitata", "si", "sì", "acceso"}
)
_FALSE_VALUES = frozenset(
    {"0", "off", "false", "no", "disabled", "disable", "disattivo", "disattivato",
     "disattivata", "disabilitato", "disabilitata", "spento", "none", "nessuno"}
)


def _as_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    text = value.strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return None


@dataclass
class ModemSettings:
    """Settings read from the gateway's configuration pages."""

    firewall_level: str | None = None
    dmz_enabled: bool | None = None
    dmz_host: str | None = None
    upnp_enabled: bool | None = None
    remote_access_enabled: bool | None = None
    ping_response_enabled: bool | None = None
    dhcp_enabled: bool | None = None
    dhcp_start: str | None = None
    dhcp_end: str | None = None
    dhcp_lease_time: str | None = None
    lan_ip: str | None = None
    lan_netmask: str | None = None
    # Everything that was read, per page — the fallback when a firmware names
    # a field differently than the keywords below expect.
    raw: dict[str, dict[str, str]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def dhcp_range(self) -> str | None:
        if self.dhcp_start and self.dhcp_end:
            return f"{self.dhcp_start} – {self.dhcp_end}"
        return self.dhcp_start or self.dhcp_end


def _control_value(control) -> str | None:
    """Current value of one form control, or None when it carries no state."""
    if control.name == "select":
        option = control.find("option", selected=True) or control.find("option")
        return option.get_text(strip=True) if option else None

    if control.name == "textarea":
        return control.get_text(strip=True)

    input_type = (control.get("type") or "text").strip().lower()
    if input_type == "checkbox":
        return "on" if control.has_attr("checked") else "off"
    if input_type == "radio":
        # Only the selected radio of a group says anything about the setting.
        return control.get("value") if control.has_attr("checked") else None
    if input_type in ("submit", "button", "reset", "image", "file", "password"):
        return None
    return control.get("value")


def _extract_fields(html: str) -> dict[str, str]:
    """Flatten a settings page into {field name or label: current value}.

    Keys come from both the control's ``name`` attribute (stable across
    languages) and its visible label (readable, and present when the value is
    rendered as plain text rather than as a control).
    """
    soup = BeautifulSoup(html, "html.parser")
    fields: dict[str, str] = {}

    def put(key: str | None, value: str | None) -> None:
        if not key or value is None:
            return
        normalised = " ".join(key.split()).strip().lower()
        if normalised and normalised not in fields:
            fields[normalised] = value.strip()

    for group in soup.find_all(class_="control-group"):
        # The row's own label, not the inline label of a single radio button.
        label = group.find(class_="control-label") or group.find("label")
        label_text = label.get_text(" ", strip=True).rstrip(":") if label else ""

        value: str | None = None
        for control in group.find_all(["input", "select", "textarea"]):
            control_value = _control_value(control)
            if control_value is None:
                continue
            put(control.get("name"), control_value)
            if value is None or value == "off":
                value = control_value

        if value is None:
            # Read-only rows show the value as text instead of as a control.
            shown = group.find(class_="simple-desc") or group.find("span")
            if shown:
                value = shown.get_text(" ", strip=True)

        put(label_text, value)

    # Controls outside a control-group (hidden state, plain forms).
    for control in soup.find_all(["input", "select", "textarea"]):
        put(control.get("name"), _control_value(control))

    return fields


def _find_field(fields: dict[str, str], *keyword_groups: tuple[str, ...]) -> str | None:
    """First value whose key matches at least one keyword from every group."""
    for key, value in fields.items():
        if all(any(keyword in key for keyword in group) for group in keyword_groups):
            return value
    return None


_DMZ = ("dmz",)
_ENABLED = ("enable", "enabled", "state", "stato", "attiv", "abilit", "status")
_ADDRESS = ("dest", "host", "ip", "addr", "indirizz", "target")


def _find_flag(fields: dict[str, str], topic: tuple[str, ...]) -> bool | None:
    """An on/off setting, named either 'x_enable' or just 'x' (a checkbox)."""
    explicit = _as_bool(_find_field(fields, topic, _ENABLED))
    if explicit is not None:
        return explicit

    for key, value in fields.items():
        if any(keyword in key for keyword in topic):
            flag = _as_bool(value)
            if flag is not None:
                return flag
    return None


def _settings_from_fields(
    fields: dict[str, str], raw: dict[str, dict[str, str]], errors: dict[str, str]
) -> ModemSettings:
    dmz_enabled = _find_flag(fields, _DMZ)
    dmz_host = _find_field(fields, _DMZ, _ADDRESS)
    if dmz_host and not _IPV4_RE.search(dmz_host):
        # e.g. an empty field or a "disabled" marker: not a destination host.
        dmz_host = None
    if dmz_enabled is None and dmz_host:
        dmz_enabled = True

    return ModemSettings(
        firewall_level=_find_field(fields, ("firewall",), ("level", "livello", "profil")),
        dmz_enabled=dmz_enabled,
        dmz_host=dmz_host,
        upnp_enabled=_find_flag(fields, ("upnp", "igd")),
        remote_access_enabled=_find_flag(fields, ("remote", "remot")),
        ping_response_enabled=_find_flag(fields, ("ping", "icmp")),
        dhcp_enabled=_find_flag(fields, ("dhcp",)),
        dhcp_start=_find_field(fields, ("dhcp", "pool"), ("start", "begin", "inizio", "from")),
        dhcp_end=_find_field(fields, ("dhcp", "pool"), ("end", "stop", "fine", "to", "last")),
        dhcp_lease_time=_find_field(fields, ("lease",), ("time", "tempo", "durat")),
        lan_ip=_find_field(fields, ("lan", "local", "gateway", "router"), ("ip", "addr", "indirizz")),
        lan_netmask=_find_field(fields, ("netmask", "mask", "maschera", "subnet")),
        raw=raw,
        errors=errors,
    )


class TimHubClient:
    """Async client for a TIM Hub / Technicolor gateway."""

    def __init__(self, host: str, port: int, username: str, password: str) -> None:
        self._base = f"http://{host}:{port}"
        self._username = username
        self._password = password
        # unsafe=True is required: the router is reached by IP, and the default
        # cookie jar refuses to store or send cookies for IP hosts, so the
        # session cookie from GET / would be lost and nginx would reply 403.
        self._session = aiohttp.ClientSession(
            timeout=DEFAULT_TIMEOUT,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )
        self._csrf_token: str | None = None

    async def close(self) -> None:
        await self._session.close()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def _fetch_csrf_token(self) -> str:
        try:
            async with self._session.get(self._base + "/") as resp:
                status = resp.status
                final_url = str(resp.url)
                content_type = resp.headers.get("Content-Type", "?")
                text = await resp.text()
        except aiohttp.ClientError as err:
            raise TimHubConnectionError(f"Modem non raggiungibile: {err}") from err

        _LOGGER.debug(
            "Pagina di login: HTTP %s, url finale %s, content-type %s, %s byte, "
            "cookie di sessione conservati: %s",
            status, final_url, content_type, len(text),
            [c.key for c in self._session.cookie_jar] or "NESSUNO",
        )

        token = _extract_csrf_token(text)
        if token:
            _LOGGER.debug("CSRFtoken trovato (%s caratteri)", len(token))
            return token

        # Give the user something actionable instead of a bare "not found".
        mentions_csrf = "csrf" in text.lower()
        _LOGGER.debug("Inizio della pagina ricevuta:\n%s", text[:1500])
        raise TimHubAuthError(
            f"CSRFtoken non trovato. Il modem ha risposto HTTP {status} da {final_url} "
            f"({content_type}, {len(text)} byte); la parola 'csrf' "
            f"{'compare' if mentions_csrf else 'NON compare'} nella pagina. "
            f"Attiva il logging di debug per vedere l'HTML ricevuto."
        )

    async def _post_authenticate(self, data: dict[str, str], step: str) -> dict:
        """POST to /authenticate and decode the JSON reply.

        The gateway answers with an HTML error page (not JSON) when it rejects
        the request itself — e.g. a stale CSRF token or a missing Referer — so
        decode manually and surface what actually came back.
        """
        try:
            async with self._session.post(
                self._base + "/authenticate",
                data=data,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": self._base + "/",
                    "Origin": self._base,
                },
            ) as resp:
                status = resp.status
                content_type = resp.headers.get("Content-Type", "?")
                text = await resp.text()
        except aiohttp.ClientError as err:
            raise TimHubConnectionError(
                f"[{step}] Errore di rete durante il login: {err}"
            ) from err

        _LOGGER.debug(
            "[%s] HTTP %s, content-type %s, %s byte, corpo: %s",
            step, status, content_type, len(text), text[:500],
        )

        try:
            return json.loads(text)
        except ValueError as err:
            raise TimHubAuthError(
                f"[{step}] Il modem non ha risposto in JSON (HTTP {status}, "
                f"{content_type}, {len(text)} byte). Inizio della risposta: "
                f"{text[:200]!r}"
            ) from err

    async def login(self) -> None:
        """Perform the SRP-6 login handshake."""
        token = await self._fetch_csrf_token()
        self._csrf_token = token

        user = SRPUser(self._username, self._password)
        uname, a_bytes = user.start_authentication()
        _LOGGER.debug(
            "Login step 1: utente=%r, CSRFtoken=%r, endpoint=%s/authenticate",
            uname, token, self._base,
        )

        challenge = await self._post_authenticate(
            {
                "CSRFtoken": token,
                "I": uname,
                "A": binascii.hexlify(a_bytes).decode("ascii"),
            },
            "step 1/2",
        )

        if "error" in challenge:
            raise TimHubAuthError(
                f"[step 1/2] Il modem ha rifiutato l'utente {uname!r}: "
                f"{_describe_auth_error(challenge['error'])}"
            )

        try:
            bytes_s = binascii.unhexlify(challenge["s"])
            bytes_b = binascii.unhexlify(challenge["B"])
        except (KeyError, binascii.Error) as err:
            raise TimHubAuthError(
                f"[step 1/2] Risposta di login inattesa (manca s/B): {challenge}"
            ) from err

        m_bytes = user.process_challenge(bytes_s, bytes_b)
        if m_bytes is None:
            raise TimHubAuthError("Controllo di sicurezza SRP fallito (valore B non valido)")

        _LOGGER.debug(
            "Login step 2: salt=%s byte (primo byte 0x%02x)",
            len(bytes_s), bytes_s[0] if bytes_s else 0,
        )
        result = await self._post_authenticate(
            {"CSRFtoken": token, "M": binascii.hexlify(m_bytes).decode("ascii")},
            "step 2/2",
        )

        if "error" in result:
            raise TimHubAuthError(
                f"[step 2/2] Il modem ha respinto la prova di password per l'utente "
                f"{uname!r}: {_describe_auth_error(result['error'])} — password "
                f"errata, oppure troppi tentativi falliti di recente."
            )

        try:
            host_hamk = binascii.unhexlify(result["M"])
        except (KeyError, binascii.Error) as err:
            raise TimHubAuthError(
                f"[step 2/2] Risposta di conferma login inattesa (manca M): {result}"
            ) from err

        user.verify_session(host_hamk)
        if not user.authenticated():
            raise TimHubAuthError(
                "[verifica finale] Il modem ha accettato la password ma la sua prova "
                "di ritorno (H_AMK) non corrisponde: variante SRP diversa da quella "
                "attesa per questo firmware."
            )

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    async def get_connection_status(self) -> ConnectionStatus:
        async with self._session.get(
            self._base + "/ajax/internet.lua", params={"auto_update": "true"}
        ) as resp:
            status = resp.status
            text = await resp.text()

        try:
            data = json.loads(text)
        except ValueError as err:
            # Usually means the session is no longer authorised and the gateway
            # served the login page instead of the status JSON.
            raise TimHubError(
                f"Stato connessione non in JSON (HTTP {status}, {len(text)} byte): "
                f"{text[:200]!r}"
            ) from err

        ppp_status = data.get("ppp_status")
        return ConnectionStatus(
            wan_ip=data.get("WAN_IP") or None,
            ppp_status=ppp_status,
            ppp_state=data.get("ppp_state"),
            connected=(ppp_status == "connected") if ppp_status else None,
            raw=data,
        )

    async def _get_page(self, path: str) -> str:
        """GET an authenticated page, failing loudly if the session is gone."""
        try:
            async with self._session.get(self._base + path) as resp:
                status = resp.status
                html = await resp.text()
        except aiohttp.ClientError as err:
            raise TimHubConnectionError(f"Errore di rete su {path}: {err}") from err

        if status != 200:
            raise TimHubError(f"{path} ha risposto HTTP {status}")
        if _looks_like_login_page(html):
            raise TimHubAuthError(
                f"{path} ha restituito la pagina di login: sessione non autenticata"
            )
        return html

    async def get_devices(self) -> list[NetworkDevice]:
        """Network interfaces known to the gateway, with their leased IPs."""
        html = await self._get_page(DEVICES_PATH)
        devices = _parse_devices(html)
        _LOGGER.debug(
            "Dispositivi: %s trovati (%s connessi) da %s (%s byte)",
            len(devices), sum(d.connected for d in devices), DEVICES_PATH, len(html),
        )
        if not devices:
            _LOGGER.debug("Nessun MAC nella pagina dispositivi; inizio:\n%s", html[:1500])
        return devices

    async def get_settings(self) -> ModemSettings:
        """Firewall level, DMZ, DHCP and the other configuration values.

        One unreadable page must not hide the settings on the others, so
        failures are recorded per page and the rest is still returned.
        """
        merged: dict[str, str] = {}
        raw: dict[str, dict[str, str]] = {}
        errors: dict[str, str] = {}

        for name, path in SETTINGS_PATHS.items():
            try:
                html = await self._get_page(path)
            except TimHubError as err:
                errors[name] = str(err)
                _LOGGER.debug("Impostazioni: %s non leggibile: %s", path, err)
                continue

            fields = _extract_fields(html)
            raw[name] = fields
            for key, value in fields.items():
                merged.setdefault(key, value)
            _LOGGER.debug("Impostazioni: %s -> %s campi %s", path, len(fields), sorted(fields))

        return _settings_from_fields(merged, raw, errors)

    async def get_call_log(self) -> CallLogResult:
        async with self._session.get(self._base + "/modals/mmpbx-log-modal.lp") as resp:
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        result = CallLogResult()

        calllog_table = soup.find("table", id="calllog")
        if calllog_table:
            result.headers = [
                th.get_text(strip=True) for th in calllog_table.find_all("th")
            ]
            mapping = _map_columns(result.headers)
            _LOGGER.debug(
                "Registro chiamate: intestazioni %s -> mappatura colonne %s",
                result.headers, mapping,
            )

            for row in calllog_table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if not cells:
                    continue  # riga di intestazione

                entry = _entry_from_cells(cells, mapping)
                if entry is not None:
                    result.entries.append(entry)

        if result.entries:
            _LOGGER.debug(
                "Registro chiamate: %s voci, tipi grezzi trovati: %s",
                len(result.entries),
                sorted({f"{e.call_type!r}->{e.kind}" for e in result.entries}),
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
