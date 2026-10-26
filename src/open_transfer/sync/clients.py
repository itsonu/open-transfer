"""iPhones and iPads in the network, through Shortcuts.

iOS has no background clipboard access for other apps and suspends them, and
Open Transfer has no iOS app. The compliant, least-interactive option is two
Shortcuts the owner runs from Back Tap, the Action button or the share sheet:

- **Send clipboard:** ``POST /api/sync/client/push`` with the clipboard's text.
- **Paste from my devices:** ``GET /api/sync/client/latest``, then "Copy to Clipboard".

Each iPhone or iPad is enrolled on one app (its *hub*) and gets a random token;
only the token's SHA-256 is stored. The hub sends its pushes into the network
on its behalf and keeps the newest clip from any member in a per-device
mailbox (in memory, until it expires). Removing the device deletes its token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from open_transfer.devices import clean_name, new_device_id, valid_id, write_private

log = logging.getLogger("open_transfer.sync")

MAX_CLIENTS = 20


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ClientStore:
    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "sync-clients.json"
        self._lock = threading.Lock()
        self._clients: dict[str, dict[str, Any]] = {}
        self._mail: dict[str, dict[str, Any]] = {}  # newest clip per client (memory only)
        self._load()

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "id": i,
                    "name": c["name"],
                    "added": c["added"],
                    "last_seen": c.get("last_seen", 0),
                }
                for i, c in sorted(self._clients.items(), key=lambda kv: kv[1]["name"].lower())
            ]

    def get(self, client_id: str) -> dict[str, Any] | None:
        with self._lock:
            client = self._clients.get(client_id)
            return None if client is None else {"id": client_id, "name": client["name"]}

    def add(self, name: str) -> tuple[dict[str, Any], str]:
        token = secrets.token_urlsafe(32)
        client_id = new_device_id()
        with self._lock:
            if len(self._clients) >= MAX_CLIENTS:
                raise ValueError(f"Up to {MAX_CLIENTS} iPhones and iPads per device.")
            self._clients[client_id] = {
                "name": clean_name(name, "iPhone"),
                "token": _digest(token),
                "added": time.time(),
            }
            self._save()
        log.info("Added an iPhone/iPad for clipboard sync")
        return {"id": client_id, "name": self._clients[client_id]["name"]}, token

    def remove(self, client_id: str) -> bool:
        with self._lock:
            removed = self._clients.pop(client_id, None) is not None
            self._mail.pop(client_id, None)
            if removed:
                self._save()
        return removed

    def authenticate(self, token: str) -> dict[str, Any] | None:
        """The client this bearer token belongs to (constant-time comparison)."""
        if not token or len(token) > 200:
            return None
        digest = _digest(token)
        found = None
        with self._lock:
            for client_id, client in self._clients.items():
                if hmac.compare_digest(client["token"], digest):
                    found = client_id
            if found is None:
                return None
            self._clients[found]["last_seen"] = time.time()
            return {"id": found, "name": self._clients[found]["name"]}

    # ------------------------------------------------------------ mailboxes

    def offer(self, client_id: str, clip: dict[str, Any]) -> bool:
        """Keep ``clip`` for ``client_id`` if it's newer than what waits there."""
        with self._lock:
            if client_id not in self._clients:
                return False
            current = self._mail.get(client_id)
            if (
                current is not None
                and current["order"] >= clip["order"]
                and current["expires"] > time.time()
            ):
                return False
            self._mail[client_id] = dict(clip)
            return True

    def latest(self, client_id: str, now: float) -> dict[str, Any] | None:
        with self._lock:
            clip = self._mail.get(client_id)
            if clip is None or clip["expires"] < now:
                self._mail.pop(client_id, None)
                return None
            return dict(clip)

    # ------------------------------------------------------------- storage

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for client_id, client in data.items():
            if (
                valid_id(client_id)
                and isinstance(client, dict)
                and isinstance(client.get("token"), str)
                and len(client["token"]) == 64
            ):
                self._clients[client_id] = {
                    "name": clean_name(client.get("name"), "iPhone"),
                    "token": client["token"],
                    "added": float(client.get("added") or 0),
                }

    def _save(self) -> None:
        try:
            write_private(self._path, self._clients)
        except OSError:
            log.warning("Could not save iPhones and iPads to %s", self._path)
