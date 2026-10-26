"""Seal a payload for one recipient with the key both devices share.

Standard library only (the project adds no runtime dependencies), built from
standard parts:

- **HKDF-SHA256** (RFC 5869) derives separate encryption and MAC keys from
  the pair key.
- **Encryption:** the payload is XORed with an HMAC-SHA256 counter-mode
  keystream (a PRF in CTR mode) under a fresh random 128-bit nonce.
- **Encrypt-then-MAC:** an HMAC-SHA256 tag covers the header (canonical
  JSON), the nonce and the ciphertext, and is checked in constant time before
  anything is decrypted.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from typing import Any

_LABEL = b"open-transfer-sync/1"
NONCE_BYTES = 16


class SealError(ValueError):
    """The sealed payload is malformed, tampered with, or for another key."""


def hkdf(key: bytes, *, info: bytes, length: int = 32, salt: bytes = b"") -> bytes:
    prk = hmac.digest(salt or bytes(32), key, "sha256")
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.digest(prk, block + info + bytes([counter]), "sha256")
        out += block
        counter += 1
    return out[:length]


def _keys(key: bytes) -> tuple[bytes, bytes]:
    return hkdf(key, info=_LABEL + b" enc"), hkdf(key, info=_LABEL + b" mac")


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    blocks = (length + 31) // 32
    return b"".join(
        hmac.digest(enc_key, nonce + i.to_bytes(8, "big"), "sha256") for i in range(blocks)
    )[:length]


def _xor(data: bytes, stream: bytes) -> bytes:
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(len(data), "big")


def _tag(mac_key: bytes, header: dict[str, Any], nonce: bytes, ct: bytes) -> bytes:
    aad = json.dumps(header, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    msg = _LABEL + len(aad).to_bytes(8, "big") + aad + nonce + hashlib.sha256(ct).digest()
    return hmac.digest(mac_key, msg, "sha256")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def seal(key: bytes, header: dict[str, Any], plaintext: bytes) -> dict[str, str]:
    enc_key, mac_key = _keys(key)
    nonce = secrets.token_bytes(NONCE_BYTES)
    ct = _xor(plaintext, _keystream(enc_key, nonce, len(plaintext)))
    return {"nonce": _b64(nonce), "ct": _b64(ct), "tag": _b64(_tag(mac_key, header, nonce, ct))}


def open_sealed(key: bytes, header: dict[str, Any], sealed: dict[str, Any]) -> bytes:
    try:
        nonce = base64.b64decode(str(sealed["nonce"]), validate=True)
        ct = base64.b64decode(str(sealed["ct"]), validate=True)
        tag = base64.b64decode(str(sealed["tag"]), validate=True)
    except (KeyError, TypeError, binascii.Error) as exc:
        raise SealError("malformed") from exc
    if len(nonce) != NONCE_BYTES:
        raise SealError("malformed")
    enc_key, mac_key = _keys(key)
    if not hmac.compare_digest(_tag(mac_key, header, nonce, ct), tag):
        raise SealError("bad tag")
    return _xor(ct, _keystream(enc_key, nonce, len(ct)))
