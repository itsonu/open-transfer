"""Who this device is, and which other devices it trusts.

Every Open Transfer app has a stable random **device id** (kept in
``<state>/device.json``) and a human name. Pairing two devices (SRP, see
:mod:`open_transfer.srp` and docs/trust-model.md) stores a shared secret key on
both sides (``<state>/trusted.json``); requests between paired devices, and
their answers, are signed with it (:func:`sign`, :func:`sign_response`), which
lets them skip the "Accept?" prompt and proves who is at an address.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("open_transfer")

FORMS = ("computer", "phone", "tablet")
PLATFORMS = ("windows", "macos", "linux", "android", "ios", "chromeos", "unknown")
MAX_NAME_CHARS = 40
#: How far a signed request's timestamp may drift from our clock.
SIGNATURE_WINDOW = 120.0

_ID_RE = re.compile(r"^[dw]-[0-9a-f]{8,32}$")


def detect_platform() -> str:
    plat = str(sys.platform)
    if plat == "android" or ("ANDROID_ROOT" in os.environ and "ANDROID_DATA" in os.environ):
        return "android"
    if plat.startswith("win"):
        return "windows"
    if plat == "darwin":
        return "macos"
    if plat.startswith("linux"):
        return "linux"
    return "unknown"


def clean_name(value: object, fallback: str = "Unnamed device") -> str:
    """A display name that is safe to show and log: no control characters, max 40 chars."""
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(value or "")))
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = re.sub(r" +", " ", text).strip()
    return text[:MAX_NAME_CHARS].strip() or fallback


def clean_form(value: object) -> str:
    return str(value) if value in FORMS else "computer"


def clean_platform(value: object) -> str:
    return str(value) if value in PLATFORMS else "unknown"


def valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def new_device_id() -> str:
    return "d-" + secrets.token_hex(12)


def new_visitor_id() -> str:
    return "w-" + secrets.token_hex(12)


def _write_private(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON readable only by this user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@dataclass
class Identity:
    """This device, as other devices see it."""

    id: str
    name: str
    form: str
    platform: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IdentityStore:
    """Loads (or creates) this device's id and lets the owner rename it."""

    def __init__(
        self, state_dir: Path, *, name: str | None, form: str, platform: str | None
    ) -> None:
        self._path = state_dir / "device.json"
        self._lock = threading.Lock()
        stored = _read_json(self._path)
        device_id = stored.get("id") if valid_id(stored.get("id")) else new_device_id()
        default_name = stored.get("name") or _default_name()
        self.identity = Identity(
            id=str(device_id),
            name=clean_name(name or default_name),
            form=clean_form(form),
            platform=clean_platform(platform or detect_platform()),
        )
        self._name_locked = bool(name)
        if stored.get("id") != device_id:
            self._save()

    def rename(self, name: str) -> Identity:
        with self._lock:
            self.identity.name = clean_name(name)
            self._save()
        return self.identity

    def _save(self) -> None:
        try:
            _write_private(self._path, {"id": self.identity.id, "name": self.identity.name})
        except OSError:  # read-only volume: identity lives for this run only
            log.debug("could not save the device identity", exc_info=True)


def _default_name() -> str:
    from open_transfer import network

    return network.hostname()


@dataclass
class TrustedDevice:
    id: str
    name: str
    key: str  # hex
    paired_at: float
    form: str = "computer"
    platform: str = "unknown"
    last_seen: float = 0.0  # wall clock, last time it proved itself


class TrustStore:
    """Devices this one is paired with, and the key each pair shares."""

    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "trusted.json"
        self._lock = threading.Lock()
        self._devices: dict[str, TrustedDevice] = {}
        for device_id, item in _read_json(self._path).items():
            try:
                if valid_id(device_id) and len(bytes.fromhex(item["key"])) >= 16:
                    self._devices[device_id] = TrustedDevice(
                        device_id,
                        clean_name(item.get("name")),
                        item["key"],
                        float(item.get("paired_at", 0)),
                        clean_form(item.get("form")),
                        clean_platform(item.get("platform")),
                        float(item.get("last_seen", 0)),
                    )
            except (KeyError, TypeError, ValueError):
                continue

    def get(self, device_id: str) -> TrustedDevice | None:
        with self._lock:
            return self._devices.get(device_id)

    def ids(self) -> set[str]:
        with self._lock:
            return set(self._devices)

    def all(self) -> list[TrustedDevice]:
        with self._lock:
            return list(self._devices.values())

    def add(
        self,
        device_id: str,
        name: str,
        key: bytes,
        *,
        form: str = "computer",
        platform: str = "unknown",
    ) -> TrustedDevice:
        now = time.time()
        device = TrustedDevice(
            device_id,
            clean_name(name),
            key.hex(),
            now,
            clean_form(form),
            clean_platform(platform),
            now,
        )
        with self._lock:
            self._devices[device_id] = device
            self._save()
        return device

    def rename(self, device_id: str, name: str) -> None:
        with self._lock:
            device = self._devices.get(device_id)
            if device and device.name != clean_name(name):
                device.name = clean_name(name)
                self._save()

    def seen(self, device_id: str, *, form: str, platform: str) -> None:
        """A paired device proved itself: remember what it is and when (saved now and then)."""
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                return
            changed = (device.form, device.platform) != (form, platform)
            device.form, device.platform = clean_form(form), clean_platform(platform)
            stale = time.time() - device.last_seen > 600
            device.last_seen = time.time()
            if changed or stale:
                self._save()

    def remove(self, device_id: str) -> bool:
        with self._lock:
            removed = self._devices.pop(device_id, None) is not None
            if removed:
                self._save()
        return removed

    def _save(self) -> None:
        data = {
            d.id: {
                "name": d.name, "key": d.key, "paired_at": d.paired_at,
                "form": d.form, "platform": d.platform, "last_seen": d.last_seen,
            }
            for d in self._devices.values()
        }  # fmt: skip
        try:
            _write_private(self._path, data)
        except OSError:
            log.warning("Could not save paired devices to %s", self._path)


# ------------------------------------------------------------------ signing


def _message(stamp: str, method: str, path: str, sender: str, body: bytes) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return f"open-transfer/2\n{stamp}\n{method.upper()}\n{path}\n{sender}\n{digest}".encode()


def sign(key: bytes, method: str, path: str, sender: str, body: bytes = b"") -> str:
    """Value for the ``X-OT-Auth`` header of a request to a paired device.

    ``<time>.<nonce>:<mac>``: the random nonce keeps two identical requests sent
    in the same millisecond from looking like a replay of each other.
    """
    stamp = f"{time.time():.3f}.{secrets.token_hex(8)}"
    mac = hmac.new(key, _message(stamp, method, path, sender, body), hashlib.sha256)
    return f"{stamp}:{mac.hexdigest()}"


class SignatureChecker:
    """Verifies ``X-OT-Auth`` headers and refuses replays of the same signature."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def verify(
        self, key: bytes, header: str | None, method: str, path: str, sender: str, body: bytes
    ) -> bool:
        if not header or ":" not in header:
            return False
        stamp, _, mac = header.partition(":")
        seconds, _, nonce = stamp.rpartition(".")
        try:
            sent_at = float(seconds)
        except ValueError:
            return False
        if len(nonce) != 16:
            return False
        now = time.time()
        if abs(now - sent_at) > SIGNATURE_WINDOW:
            return False
        expected = hmac.new(key, _message(stamp, method, path, sender, body), hashlib.sha256)
        if not hmac.compare_digest(expected.hexdigest(), mac):
            return False
        with self._lock:
            for seen, at in list(self._seen.items()):
                if now - at > 2 * SIGNATURE_WINDOW:
                    del self._seen[seen]
            if mac in self._seen:
                return False
            self._seen[mac] = now
        return True


def sign_response(key: bytes, request_auth: str, status: int, body: bytes) -> str:
    """``X-OT-Auth`` for an answer to a signed request: binds it to that request."""
    message = (
        f"open-transfer/1\nresponse\n{request_auth}\n{status}\n{hashlib.sha256(body).hexdigest()}"
    )
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def verify_response(
    key: bytes, request_auth: str, status: int, body: bytes, header: str | None
) -> bool:
    return bool(header) and hmac.compare_digest(
        sign_response(key, request_auth, status, body), str(header)
    )


# ------------------------------------------------------------------ pairing
#
# SRP-6a over the 6-digit code (open_transfer.srp, docs/trust-model.md). The
# SRP session key K never leaves either device; the pair key stored on both is
# derived from it, and the "allowed?" polling in between is MAC'd with K.


def derive_pair_key(session_key: bytes, id_a: str, id_b: str) -> bytes:
    """The long-term key for the pair (id_a shows the code, id_b enters it)."""
    message = f"open-transfer-pair-key/2|{id_a}|{id_b}".encode()
    return hmac.new(session_key, message, hashlib.sha256).digest()


def session_mac(session_key: bytes, *parts: str) -> str:
    message = "open-transfer-pair/2|" + "|".join(parts)
    return hmac.new(session_key, message.encode(), hashlib.sha256).hexdigest()


def new_pair_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"
