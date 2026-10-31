"""Encrypt files between paired apps: ``ot-file/1`` (docs/encryption.md).

Vetted parts only, from ``cryptography``: HKDF-SHA256 derives a key per file
from the pair key, and AES-256-GCM seals the file in 64 KiB records (the
STREAM construction: nonce = record number ‖ last-record flag). Records can't
be changed, moved, replayed elsewhere or cut off without failing to decrypt.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = 1
RECORD = 64 * 1024
TAG = 16


class DecryptError(ValueError):
    """Wrong key, or the bytes were changed, reordered, replayed or cut short."""


class Truncated(DecryptError):
    """The stream ended before the last record: interrupted, or cut off on purpose."""


class Readable(Protocol):
    def read(self, n: int = -1, /) -> bytes: ...


def new_salt() -> str:
    return secrets.token_hex(32)


def hkdf(ikm: bytes, *, salt: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(hashes.SHA256(), length, salt or None, info).derive(ikm)


def file_key(
    pair_key: bytes, salt: bytes, sender: str, receiver: str, target: str, index: int, size: int
) -> bytes:
    info = f"open-transfer-file/{VERSION}|{sender}|{receiver}|{target}|{index}|{size}"
    return hkdf(pair_key, salt=salt, info=info.encode())


def meta_key(pair_key: bytes, salt: bytes, sender: str, receiver: str, target: str) -> bytes:
    info = f"open-transfer-meta/{VERSION}|{sender}|{receiver}|{target}"
    return hkdf(pair_key, salt=salt, info=info.encode())


def verify_mac(key: bytes, sha256_hex: str) -> str:
    """Confirms a file's checksum without showing it (a bare hash lets a listener test guesses)."""
    mac_key = hkdf(key, salt=b"", info=f"open-transfer-verify/{VERSION}".encode())
    return hmac.new(mac_key, sha256_hex.lower().encode(), hashlib.sha256).hexdigest()


def sealed_size(size: int) -> int:
    return size + TAG * _records(size)


def nonce(index: int, last: bool) -> bytes:
    return index.to_bytes(11, "big") + (b"\x01" if last else b"\x00")


def encrypt_record(key: bytes, index: int, last: bool, data: bytes) -> bytes:
    return AESGCM(key).encrypt(nonce(index, last), data, None)


def _records(size: int) -> int:
    return max(1, -(-size // RECORD))


class Cipher:
    """One file's key and size; encrypts and decrypts whole records anywhere in it."""

    def __init__(self, key: bytes, size: int) -> None:
        self.key = key
        self.size = size
        self.records = _records(size)
        self._aead = AESGCM(key)

    def plain_len(self, index: int) -> int:
        return RECORD if index < self.records - 1 else self.size - RECORD * (self.records - 1)

    def encrypt(self, index: int, data: bytes) -> bytes:
        if not 0 <= index < self.records or len(data) != self.plain_len(index):
            raise ValueError("not a whole record of this file")
        return self._aead.encrypt(nonce(index, index == self.records - 1), data, None)

    def decrypt(self, index: int, data: bytes) -> bytes:
        if not 0 <= index < self.records or len(data) != self.plain_len(index) + TAG:
            raise DecryptError("record out of place")
        try:
            return self._aead.decrypt(nonce(index, index == self.records - 1), data, None)
        except InvalidTag as exc:
            raise DecryptError("record failed authentication") from exc

    def encrypt_range(self, offset: int, data: bytes) -> bytes:
        """Whole records starting at plaintext ``offset`` (a multiple of RECORD)."""
        if offset % RECORD:
            raise ValueError("not at a record boundary")
        first, pos = offset // RECORD, 0
        out: list[bytes] = []
        while pos < len(data) or not out:
            n = self.plain_len(first + len(out)) if first + len(out) < self.records else 0
            out.append(self.encrypt(first + len(out), data[pos : pos + n]))
            pos += n
        return b"".join(out)

    def decrypt_range(self, offset: int, data: bytes) -> bytes:
        """Inverse of ``encrypt_range``: raises DecryptError unless every record is genuine."""
        if offset % RECORD:
            raise DecryptError("not at a record boundary")
        first, pos = offset // RECORD, 0
        out: list[bytes] = []
        while pos < len(data) or not out:
            index = first + len(out)
            n = self.plain_len(index) + TAG if index < self.records else len(data) - pos
            out.append(self.decrypt(index, data[pos : pos + n]))
            pos += n
        return b"".join(out)


class _Records:
    """Turns a stream into one record per ``read``; ``read_all`` for tests."""

    def __init__(self, source: Readable, cipher: Cipher) -> None:
        self.source, self.cipher = source, cipher
        self._index, self._buf, self._pos = 0, b"", 0

    def _next(self) -> bytes:
        raise NotImplementedError

    def read(self, n: int = -1, /) -> bytes:
        if self._pos >= len(self._buf):
            if self._index >= self.cipher.records:
                return b""
            self._buf, self._pos = self._next(), 0
            self._index += 1
        end = len(self._buf) if n < 0 else self._pos + n
        out = self._buf[self._pos : end]
        self._pos += len(out)
        return out

    def read_all(self) -> bytes:
        return b"".join(iter(lambda: self.read(1 << 20), b""))


class Encrypting(_Records):
    """Plaintext stream in, ciphertext out (``sealed_size(size)`` bytes)."""

    def _next(self) -> bytes:
        want = self.cipher.plain_len(self._index)
        data = _read_exact(self.source, want)
        if len(data) != want:
            raise OSError("the file ended before its size")
        return self.cipher.encrypt(self._index, data)


class Decrypting(_Records):
    """Ciphertext stream in, plaintext out; raises DecryptError on anything not genuine."""

    def _next(self) -> bytes:
        want = self.cipher.plain_len(self._index) + TAG
        data = _read_exact(self.source, want)
        if len(data) != want:
            raise Truncated("cut short")
        return self.cipher.decrypt(self._index, data)


def _read_exact(source: Readable, n: int) -> bytes:
    parts, got = [], 0
    while got < n:
        chunk = source.read(n - got)
        if not chunk:
            break
        parts.append(chunk)
        got += len(chunk)
    return b"".join(parts)


def seal_meta(key: bytes, value: Any) -> str:
    iv = secrets.token_bytes(12)
    data = json.dumps(value, separators=(",", ":")).encode()
    return base64.b64encode(iv + AESGCM(key).encrypt(iv, data, None)).decode()


def open_meta(key: bytes, sealed: str) -> Any:
    try:
        raw = base64.b64decode(sealed, validate=True)
        return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], None))
    except (InvalidTag, binascii.Error, ValueError, TypeError) as exc:
        raise DecryptError("metadata failed authentication") from exc
