"""SRP-6 client implementation for Technicolor gateways (TIM Hub / Hub+).

Ported from the verified, community-tested implementation in
https://pypi.org/project/pytechnicolor/ (itself derived from
https://github.com/cocagne/pysrp), which is confirmed to work against
TIM Hub (Technicolor AGHP/DGA4132 and similar) gateways.

Note this is SRP-6 (fixed multiplier k), not SRP-6a (k = H(N, g)) — the
router firmware uses the older fixed-k variant, so we replicate that
exactly rather than using a generic/standard SRP-6a library.
"""
from __future__ import annotations

import hashlib
import operator
import os

SHA256 = "sha256"

# RFC 5054, 2048-bit group (N, g). This is the group used by the router.
_N_HEX = (
    "AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC3192943DB56050A373"
    "29CBB4A099ED8193E0757767A13DD52312AB4B03310DCD7F48A9DA04FD50E80839"
    "69EDB767B0CF6095179A163AB3661A05FBD5FAAAE82918A9962F0B93B855F97993"
    "EC975EEAA80D740ADBF4FF747359D041D5C33EA71D281E446B14773BCA97B43A23"
    "FB801676BD207A436C6481F1D2B9078717461A5B9D32E688F87748544523B524B0"
    "D57D5EA77A2775D2ECFA032CFBDBF52FB3786160279004E57AE6AF874E7303CE53"
    "299CCC041C7BC308D82A5698F3A8D0C38271AE35F8E9DBFBB694B5C803D89F7AE4"
    "35DE236D525F54759B65E372FCD68EF20FA7111F9E4AFF73"
)
_G_HEX = "2"

# Fixed multiplier 'k' used by this router's SRP-6 variant (not H(N,g) as
# in SRP-6a). This value comes directly from the verified reference
# implementation.
_K = int("05b9e8ef059c6b32ea59fc1d322d37f04aa30bae5aa9003b8321e21ddb04e300", 16)


def _bytes_to_long(b: bytes) -> int:
    n = 0
    for byte in b:
        n = (n << 8) | byte
    return n


def _long_to_bytes(n: int) -> bytes:
    out = bytearray()
    x = 0
    off = 0
    while x != n:
        byte = (n >> off) & 0xFF
        out.append(byte)
        x |= byte << off
        off += 8
    out.reverse()
    return bytes(out)


def _get_random_of_length(nbytes: int) -> int:
    offset = (nbytes * 8) - 1
    return _bytes_to_long(os.urandom(nbytes)) | (1 << offset)


def _h(*args: int | str | bytes) -> int:
    """Concatenate args (as bytes) and return SHA-256 digest as an int."""
    hasher = hashlib.sha256()
    for value in args:
        if value is None:
            continue
        if isinstance(value, int):
            hasher.update(_long_to_bytes(value))
        elif isinstance(value, str):
            hasher.update(value.encode("latin-1"))
        else:
            hasher.update(value)
    return int(hasher.hexdigest(), 16)


def _h_n_xor_g(n: int, g: int) -> bytes:
    h_n = hashlib.sha256(_long_to_bytes(n)).digest()
    h_g = hashlib.sha256(_long_to_bytes(g)).digest()
    return bytes(operator.xor(a, b) for a, b in zip(h_n, h_g, strict=True))


class SRPUser:
    """Client-side SRP-6 session, matching this router's exact variant."""

    def __init__(self, username: str, password: str) -> None:
        self.N = int(_N_HEX, 16)
        self.g = int(_G_HEX, 16)
        self.k = _K

        self.I = username  # noqa: E741 (matches SRP spec naming)
        self.p = password
        self.a = _get_random_of_length(32)
        self.A = pow(self.g, self.a, self.N)

        self.s: bytes | None = None
        self.B: int | None = None
        self.K: bytes | None = None
        self.M: bytes | None = None
        self.H_AMK: bytes | None = None
        self._authenticated = False

    def start_authentication(self) -> tuple[str, bytes]:
        """Return (username, A) to send as the first request."""
        return self.I, _long_to_bytes(self.A)

    def process_challenge(self, bytes_s: bytes, bytes_b: bytes) -> bytes | None:
        """Given server salt + B, compute and return M (proof) to send back."""
        # Keep the salt as raw bytes: converting it to an int and back would
        # strip any leading zero bytes, producing a different x and M than the
        # server computed (pysrp keeps it as bytes for exactly this reason).
        self.s = bytes_s
        self.B = _bytes_to_long(bytes_b)

        if (self.B % self.N) == 0:
            return None  # SRP-6a safety check

        u = _h(self.A, self.B)
        if u == 0:
            return None  # SRP-6a safety check

        x = _h(self.s, _h(f"{self.I}:{self.p}"))
        v = pow(self.g, x, self.N)

        s_val = pow((self.B - self.k * v) % self.N, (self.a + u * x), self.N)
        self.K = hashlib.sha256(_long_to_bytes(s_val)).digest()

        hasher = hashlib.sha256()
        hasher.update(_h_n_xor_g(self.N, self.g))
        hasher.update(hashlib.sha256(self.I.encode("latin-1")).digest())
        hasher.update(self.s)
        hasher.update(_long_to_bytes(self.A))
        hasher.update(_long_to_bytes(self.B))
        hasher.update(self.K)
        self.M = hasher.digest()

        hasher2 = hashlib.sha256()
        hasher2.update(_long_to_bytes(self.A))
        hasher2.update(self.M)
        hasher2.update(self.K)
        self.H_AMK = hasher2.digest()

        return self.M

    def verify_session(self, host_hamk: bytes) -> None:
        if self.H_AMK == host_hamk:
            self._authenticated = True

    def authenticated(self) -> bool:
        return self._authenticated
