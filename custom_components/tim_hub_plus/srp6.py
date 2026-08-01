"""SRP-6 client for Technicolor gateways (TIM Hub / Hub+).

Replicates, byte for byte, the client the gateway itself serves at
``/js/srp-min.js`` (funzione ``SRP()``). Every step below was verified
against that code running in Node with fixed inputs, because the details
that differ from a textbook SRP-6 client are exactly the ones that make
the router answer ``M didn't match``:

* strings (``I``, ``I:P``) are hashed as **UTF-8**, not latin-1;
* ``u = H(PAD(A) || PAD(B))`` with both values left-padded to 256 bytes
  (``q`` in the minified source);
* ``s`` and ``B`` go into ``M`` exactly as the server sent them, so a
  leading zero byte must not be dropped — which is what happens if they
  are round-tripped through an integer;
* ``A``, ``S`` are hashed as their minimal big-endian encoding instead.

This is SRP-6 with a fixed multiplier k (not SRP-6a's k = H(N, g)).
"""
from __future__ import annotations

import hashlib
import os

# RFC 5054, 2048-bit group, as hardcoded in srp-min.js.
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
_N = int(_N_HEX, 16)
_G = 2

# Fixed multiplier k, copied from srp-min.js (there: the BigInteger "C").
_K = int("05b9e8ef059c6b32ea59fc1d322d37f04aa30bae5aa9003b8321e21ddb04e300", 16)

# Width A and B are padded to before hashing them into u ("q" in the JS).
_PAD_WIDTH = 256

# Private exponent size, matching "new BigInteger(256, w)" in the JS.
_EXPONENT_BITS = 256


def _sha256(*chunks: bytes) -> bytes:
    hasher = hashlib.sha256()
    for chunk in chunks:
        hasher.update(chunk)
    return hasher.digest()


def _long_to_bytes(value: int) -> bytes:
    """Minimal big-endian encoding, i.e. the JS ``toString(16)`` padded even."""
    return value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")


def _pad(value: int) -> bytes:
    return value.to_bytes(_PAD_WIDTH, "big")


# H(N) xor H(g): the JS ships this precomputed as the constant "u"; the value
# below reproduces it exactly (checked against 4a76a9a2...4b29cc4c).
_H_N_XOR_G = bytes(
    a ^ b
    for a, b in zip(
        _sha256(_long_to_bytes(_N)), _sha256(_long_to_bytes(_G)), strict=True
    )
)


class SRPUser:
    """Client-side SRP-6 session, matching this router's exact variant."""

    def __init__(self, username: str, password: str) -> None:
        self.N = _N
        self.g = _G
        self.k = _K

        self.I = username  # noqa: E741 (matches SRP spec naming)
        self.p = password
        self.a = int.from_bytes(os.urandom(_EXPONENT_BITS // 8), "big")
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
        self.s = bytes_s
        self.B = int.from_bytes(bytes_b, "big")

        if (self.B % self.N) == 0:
            return None  # safety check, as in the router's own client

        u = int.from_bytes(_sha256(_pad(self.A), _pad(self.B)), "big")
        if u == 0:
            return None

        x = int.from_bytes(
            _sha256(bytes_s, _sha256(f"{self.I}:{self.p}".encode())), "big"
        )
        v = pow(self.g, x, self.N)

        exponent = (self.a + (u * x) % self.N) % self.N
        s_val = pow((self.B - self.k * v) % self.N, exponent, self.N)
        self.K = _sha256(_long_to_bytes(s_val))

        self.M = _sha256(
            _H_N_XOR_G,
            _sha256(self.I.encode()),
            bytes_s,
            _long_to_bytes(self.A),
            bytes_b,
            self.K,
        )
        self.H_AMK = _sha256(_long_to_bytes(self.A), self.M, self.K)

        return self.M

    def verify_session(self, host_hamk: bytes) -> None:
        if self.H_AMK == host_hamk:
            self._authenticated = True

    def authenticated(self) -> bool:
        return self._authenticated
