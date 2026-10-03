"""SRP-6a (RFC 5054): agree on a key from a short shared code, safely.

Both devices know the 6-digit pairing code. SRP turns that into a strong
shared key such that someone watching the exchange learns nothing they can
test guesses against offline, and someone pretending to be one of the devices
gets exactly one guess per attempt. See docs/trust-model.md.

The device *showing* the code plays the SRP server (it derives the verifier
from the code on the fly), the device *entering* it plays the client.
2048-bit group from RFC 5054 Appendix A, SHA-256, with RFC 5054 padding.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

N = int(
    "AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC3192943DB56050A37329CB"
    "B4A099ED8193E0757767A13DD52312AB4B03310DCD7F48A9DA04FD50E8083969EDB767B0"
    "CF6095179A163AB3661A05FBD5FAAAE82918A9962F0B93B855F97993EC975EEAA80D740A"
    "DBF4FF747359D041D5C33EA71D281E446B14773BCA97B43A23FB801676BD207A436C6481"
    "F1D2B9078717461A5B9D32E688F87748544523B524B0D57D5EA77A2775D2ECFA032CFBDB"
    "F52FB3786160279004E57AE6AF874E7303CE53299CCC041C7BC308D82A5698F3A8D0C382"
    "71AE35F8E9DBFBB694B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F"
    "9E4AFF73",
    16,
)
G = 2
WIDTH = (N.bit_length() + 7) // 8  # 256 bytes


class SRPError(ValueError):
    """The other side sent a value that must be refused (or proved nothing)."""


def to_bytes(n: int) -> bytes:
    return n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big")


def _h(*parts: bytes | int, pad: bool = False) -> bytes:
    h = hashlib.sha256()
    for part in parts:
        data = to_bytes(part) if isinstance(part, int) else part
        if pad:
            h.update(bytes(WIDTH - len(data)))
        h.update(data)
    return h.digest()


def _int(data: bytes) -> int:
    return int.from_bytes(data, "big")


K_MULT = _int(_h(N, G, pad=True))
_HN_XOR_HG = bytes(a ^ b for a, b in zip(_h(N), _h(G, pad=True), strict=True))


def _x(salt: bytes, identity: str, code: str) -> int:
    return _int(_h(salt, _h(f"{identity}:{code}".encode())))


def _proof(identity: str, salt: bytes, a_pub: int, b_pub: int, key: bytes) -> bytes:
    return _h(_HN_XOR_HG, _h(identity.encode()), salt, a_pub, b_pub, key)


class Server:
    """The device showing the code. Make one per attempt."""

    def __init__(
        self, identity: str, code: str, *, salt: bytes | None = None, b: int | None = None
    ) -> None:
        self.identity = identity
        self.salt = salt or secrets.token_bytes(16)
        self._v = pow(G, _x(self.salt, identity, code), N)
        self._b = b if b is not None else _int(secrets.token_bytes(32))
        self.b_pub = (K_MULT * self._v + pow(G, self._b, N)) % N
        self.key: bytes | None = None

    def verify(self, a_pub: int, client_proof: bytes) -> bytes:
        """Check the client's proof; returns the server's proof (raises if wrong)."""
        if a_pub % N == 0:
            raise SRPError("invalid public value")
        u = _int(_h(a_pub, self.b_pub, pad=True))
        if u == 0:
            raise SRPError("invalid public value")
        secret = pow(a_pub * pow(self._v, u, N), self._b, N)
        key = _h(secret)
        expected = _proof(self.identity, self.salt, a_pub, self.b_pub, key)
        if not hmac.compare_digest(expected, client_proof):
            raise SRPError("wrong code")
        self.key = key
        return _h(a_pub, client_proof, key)


class Client:
    """The device entering the code."""

    def __init__(self, identity: str, code: str, *, a: int | None = None) -> None:
        self.identity = identity
        self._code = code
        self._a = a if a is not None else _int(secrets.token_bytes(32))
        self.a_pub = pow(G, self._a, N)
        self.key: bytes | None = None
        self._expected: bytes = b""

    def respond(self, salt: bytes, b_pub: int) -> bytes:
        """Answer the server's challenge; returns the client's proof."""
        if b_pub % N == 0:
            raise SRPError("invalid public value")
        u = _int(_h(self.a_pub, b_pub, pad=True))
        if u == 0:
            raise SRPError("invalid public value")
        x = _x(salt, self.identity, self._code)
        secret = pow((b_pub - K_MULT * pow(G, x, N)) % N, self._a + u * x, N)
        key = _h(secret)
        proof = _proof(self.identity, salt, self.a_pub, b_pub, key)
        self._expected = _h(self.a_pub, proof, key)
        self.key = key
        return proof

    def check(self, server_proof: bytes) -> None:
        """Raises unless the server proved it knows the code too."""
        if not self._expected or not hmac.compare_digest(self._expected, server_proof):
            raise SRPError("the other device couldn’t prove it has the code")
