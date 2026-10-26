"""What the sync engine keeps between runs, in ``<state>/sync.json``.

Policies, the next sequence number, revocation tombstones and control
messages (introductions, revocations) still waiting for an offline member.
Never clipboard contents. Readable only by this user: queued introductions
carry pair keys.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from open_transfer.devices import valid_id, write_private
from open_transfer.sync.protocol import (
    CONTROL_EXPIRY,
    DEFAULT_EXPIRY,
    MAX_EXPIRY,
    MIN_EXPIRY,
    TYPES,
    SyncError,
)

log = logging.getLogger("open_transfer.sync")

#: "Until I resume" for pauses (a date far away; JSON has no infinity).
FOREVER = 253402300799.0
MAX_BLOCKED = 50

DEFAULT_POLICY: dict[str, Any] = {
    "enabled": False,
    "target": "all",  # or "selected": only the devices switched on in Clipboard settings
    "mode": "auto",  # or "manual": only when asked (button, tile, shortcut)
    "types": list(TYPES),
    "paused_until": 0.0,
    "session_until": 0.0,
    "expiry": DEFAULT_EXPIRY,
    "sensitive": "block",  # or "allow"
    "blocked_sources": [],
    "accept_introductions": True,
}
DEFAULT_DEVICE: dict[str, Any] = {"excluded": False, "paused_until": 0.0}


def _bad(message: str) -> SyncError:
    return SyncError(400, "bad_request", message)


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def validate_policy(fields: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for name, value in fields.items():
        if name in ("enabled", "accept_introductions"):
            if not isinstance(value, bool):
                raise _bad(f"{name} is true or false.")
        elif name == "target":
            if value not in ("all", "selected"):
                raise _bad("Send to all devices or selected devices.")
        elif name == "mode":
            if value not in ("auto", "manual"):
                raise _bad("Mode is auto or manual.")
        elif name == "types":
            if not (isinstance(value, list) and value and set(value) <= set(TYPES)):
                raise _bad(f"Content types are some of: {', '.join(TYPES)}.")
            value = [t for t in TYPES if t in value]
        elif name in ("paused_until", "session_until"):
            if not _number(value):
                raise _bad(f"{name} is a time.")
            value = min(float(value), FOREVER)
        elif name == "expiry":
            if not (isinstance(value, int) and MIN_EXPIRY <= value <= MAX_EXPIRY):
                raise _bad(f"Expiry is {MIN_EXPIRY} to {MAX_EXPIRY} seconds.")
        elif name == "sensitive":
            if value not in ("block", "allow"):
                raise _bad("Sensitive text is blocked or allowed.")
        elif name == "blocked_sources":
            if not (
                isinstance(value, list)
                and len(value) <= MAX_BLOCKED
                and all(isinstance(s, str) and 0 < len(s.strip()) <= 200 for s in value)
            ):
                raise _bad(f"Blocked apps: up to {MAX_BLOCKED} names.")
            value = sorted({s.strip() for s in value}, key=str.lower)
        else:
            raise _bad(f"Unknown setting {name}.")
        clean[name] = value
    return clean


class SyncStore:
    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "sync.json"
        self._lock = threading.Lock()
        self._policy: dict[str, Any] = dict(DEFAULT_POLICY, types=list(TYPES), blocked_sources=[])
        self._devices: dict[str, dict[str, Any]] = {}
        self._revoked: dict[str, float] = {}
        self._outbox: list[dict[str, Any]] = []
        # A fresh store starts at the current time in ms, so a device whose
        # sync.json was lost doesn't reuse sequence numbers others have seen.
        self._seq = int(time.time() * 1000)
        self._load()

    # ----------------------------------------------------------- policy

    def policy(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._policy))  # type: ignore[no-any-return]

    def set_policy(self, **fields: Any) -> dict[str, Any]:
        clean = validate_policy(fields)
        with self._lock:
            self._policy.update(clean)
            self._save()
        return self.policy()

    def device(self, device_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(DEFAULT_DEVICE, **self._devices.get(device_id, {}))

    def set_device(
        self, device_id: str, *, excluded: bool | None = None, paused_until: float | None = None
    ) -> dict[str, Any]:
        if excluded is not None and not isinstance(excluded, bool):
            raise _bad("excluded is true or false.")
        if paused_until is not None and not _number(paused_until):
            raise _bad("paused_until is a time.")
        with self._lock:
            entry = self._devices.setdefault(device_id, {})
            if excluded is not None:
                entry["excluded"] = excluded
            if paused_until is not None:
                entry["paused_until"] = min(float(paused_until), FOREVER)
            self._save()
        return self.device(device_id)

    def forget_device(self, device_id: str) -> None:
        with self._lock:
            if self._devices.pop(device_id, None) is not None:
                self._save()

    # ------------------------------------------------------ sequence

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            self._save()
            return self._seq

    # ---------------------------------------------------- tombstones

    def revoked_at(self, device_id: str) -> float:
        with self._lock:
            return self._revoked.get(device_id, 0.0)

    def revoke(self, device_id: str, at: float) -> None:
        with self._lock:
            self._revoked[device_id] = max(at, self._revoked.get(device_id, 0.0))
            self._devices.pop(device_id, None)
            self._outbox = [m for m in self._outbox if m["dst"] != device_id]
            self._save()

    def clear_revoked(self, device_id: str) -> None:
        with self._lock:
            if self._revoked.pop(device_id, None) is not None:
                self._save()

    # -------------------------------------------------------- outbox

    def outbox(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(m) for m in self._outbox]

    def queue(self, message: dict[str, Any]) -> None:
        with self._lock:
            self._outbox.append(message)
            self._save()

    def unqueue(self, message_id: str) -> None:
        with self._lock:
            before = len(self._outbox)
            self._outbox = [m for m in self._outbox if m["env"]["id"] != message_id]
            if len(self._outbox) != before:
                self._save()

    def drop_expired(self, now: float) -> None:
        with self._lock:
            before = len(self._outbox)
            self._outbox = [m for m in self._outbox if m["env"]["expires"] >= now]
            if len(self._outbox) != before:
                self._save()

    # ------------------------------------------------------ internals

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        policy = data.get("policy")
        if isinstance(policy, dict):
            for name, value in policy.items():
                try:
                    self._policy.update(validate_policy({name: value}))
                except SyncError:
                    log.warning("Ignoring an invalid sync setting: %s", name)
        devices = data.get("devices")
        if isinstance(devices, dict):
            for device_id, entry in devices.items():
                if valid_id(device_id) and isinstance(entry, dict):
                    self._devices[device_id] = {
                        "excluded": entry.get("excluded") is True,
                        "paused_until": float(entry["paused_until"])
                        if _number(entry.get("paused_until"))
                        else 0.0,
                    }
        revoked = data.get("revoked")
        if isinstance(revoked, dict):
            self._revoked = {k: float(v) for k, v in revoked.items() if valid_id(k) and _number(v)}
        seq = data.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool) and seq > 0:
            self._seq = max(self._seq, seq)
        outbox = data.get("outbox")
        if isinstance(outbox, list):
            now = time.time()
            self._outbox = [
                m
                for m in outbox
                if isinstance(m, dict)
                and valid_id(m.get("dst"))
                and isinstance(m.get("env"), dict)
                and _number(m["env"].get("expires"))
                and now <= m["env"]["expires"] <= now + CONTROL_EXPIRY
            ]

    def _save(self) -> None:
        try:
            write_private(
                self._path,
                {
                    "seq": self._seq,
                    "policy": self._policy,
                    "devices": self._devices,
                    "revoked": self._revoked,
                    "outbox": self._outbox,
                },
            )
        except OSError:
            log.warning("Could not save the sync settings to %s", self._path)
