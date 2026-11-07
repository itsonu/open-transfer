"""Encrypt files between apps: ``ot-file/2`` (docs/encryption.md).

Vetted parts only, from ``cryptography``: a per-transfer X25519 handshake
(:class:`Handshake`, authenticated by the pair key between paired apps) gives a
session key, HKDF-SHA256 derives a key per file from it, and AES-256-GCM seals
the file in 64 KiB records (the
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
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = 2  # ot-file/2: the ot-file/1 records, keyed by a per-transfer handshake
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


def new_kx() -> tuple[X25519PrivateKey, bytes]:
    """A fresh X25519 key pair: an app's per-run key, or a handshake's ephemeral one."""
    key = X25519PrivateKey.generate()
    return key, key.public_key().public_bytes_raw()


class Handshake:
    """One transfer's key agreement: forward secrecy (docs/encryption.md, "Forward secrecy").

    The shape of TLS 1.3's PSK + ECDHE key schedule, with established parts only
    (X25519, HKDF-SHA256, HMAC-SHA256):

    * offer: the sender's fresh X25519 key ``eph`` to the receiver's per-run key
      ``kx``. ``k1 = HKDF(DH(eph, kx) ‖ PSK)`` seals the names; between paired
      apps PSK comes from the pair key (and the offer is signed), unpaired it's zero.
    * answer: the receiver's fresh key ``eph_r`` and ``confirm = HMAC(k1, transcript)``,
      which only the holder of ``kx`` (and the pair key) can make.
    * session = ``HKDF(DH(eph, eph_r) ‖ k1)``: file, verify and signal keys derive from it.

    The transcript binds the version, both apps, the transfer, the recipient, the salt
    and every public key. Both ephemeral private keys are dropped as soon as the session
    key exists; Python can't wipe memory, so "dropped" is as far as erasure goes.
    """

    def __init__(
        self,
        pair_key: bytes | None,
        salt: bytes,
        *,
        sender: str,
        receiver: str,
        job: str,
        target: str,
    ) -> None:
        self._psk = (
            hkdf(pair_key, salt=salt, info=b"open-transfer-psk/2") if pair_key else bytes(32)
        )
        self._salt = salt
        self._ids = [sender, receiver, job, target]
        self._mine: X25519PrivateKey | None = None
        self.eph = b""
        self.meta_key = b""
        self.session = b""
        self._k1 = b""
        self._kx = b""

    def _transcript(self, eph: bytes, kx: bytes, eph_r: bytes = b"") -> bytes:
        parts = [f"open-transfer/{VERSION}", *self._ids, self._salt.hex(), eph.hex(), kx.hex(), eph_r.hex()]  # fmt: skip
        return json.dumps(parts, separators=(",", ":")).encode()

    def _keys(self, dh: bytes, eph: bytes, kx: bytes) -> None:
        t1 = self._transcript(eph, kx)
        self._k1 = hkdf(dh + self._psk, salt=self._salt, info=b"open-transfer-offer/2" + t1)
        sender, receiver, _, target = self._ids
        self.meta_key = meta_key(self._k1, self._salt, sender, receiver, target)

    def _confirm(self, t2: bytes) -> bytes:
        key = hkdf(self._k1, salt=b"", info=b"open-transfer-confirm/2")
        return hmac.new(key, t2, hashlib.sha256).digest()

    def _session(self, dh: bytes, t2: bytes) -> bytes:
        return hkdf(dh + self._k1, salt=self._salt, info=b"open-transfer-session/2" + t2)

    def offer(self, kx: bytes) -> bytes:
        """Sender: our ephemeral public key for the offer (``meta_key`` is then set)."""
        self._mine, self.eph = new_kx()
        self._kx = kx
        self._keys(_dh(self._mine, kx), self.eph, kx)
        return self.eph

    def answer(self, run_key: X25519PrivateKey, eph: bytes) -> tuple[bytes, bytes]:
        """Receiver: (our ephemeral public key, confirmation) for the answer; sets ``session``."""
        kx = run_key.public_key().public_bytes_raw()
        self._keys(_dh(run_key, eph), eph, kx)
        mine, eph_r = new_kx()
        t2 = self._transcript(eph, kx, eph_r)
        self.session = self._session(_dh(mine, eph), t2)
        del mine  # the only reference: gone with this frame
        return eph_r, self._confirm(t2)

    def finish(self, eph_r: bytes, confirm: bytes) -> bytes:
        """Sender: check the answer and return the session key."""
        mine, self._mine = self._mine, None
        if mine is None:
            raise DecryptError("no offer")
        t2 = self._transcript(self.eph, self._kx, eph_r)
        if not hmac.compare_digest(self._confirm(t2), confirm):
            raise DecryptError("the answer isn't from the receiver this offer was for")
        self.session = self._session(_dh(mine, eph_r), t2)
        return self.session


def _dh(mine: X25519PrivateKey, theirs: bytes) -> bytes:
    try:
        return mine.exchange(X25519PublicKey.from_public_bytes(theirs))
    except ValueError as exc:  # wrong length, or a point that gives an all-zero secret
        raise DecryptError("bad key") from exc


def signal_key(
    base: bytes, salt: bytes, sender: str, receiver: str, target: str, direction: str
) -> bytes:
    """MAC key for one direction of a transfer's WebRTC connection messages: whoever can
    change an SDP (its DTLS fingerprint, ICE candidates) between the apps could sit in the middle."""
    info = f"open-transfer-signal/{VERSION}|{direction}|{sender}|{receiver}|{target}"
    return hkdf(base, salt=salt, info=info.encode())


def signal_mac(key: bytes, fields: list[Any]) -> str:
    data = json.dumps(fields, separators=(",", ":")).encode()
    return hmac.new(key, data, hashlib.sha256).hexdigest()


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
