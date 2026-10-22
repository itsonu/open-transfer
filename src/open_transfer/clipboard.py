"""Clipboard history and sync settings, kept by each app in its state folder.

Items are text or images that were shared between this device and its paired
devices. Anything that looks sensitive (see :mod:`open_transfer.sensitive`) is
held in memory for a couple of minutes, so it can be copied, and never written
to disk.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from open_transfer.devices import valid_id, write_private
from open_transfer.sensitive import looks_sensitive

log = logging.getLogger("open_transfer.clipboard")

MAX_TEXT_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_MIME = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
TRANSIENT_SECONDS = 120.0
DEFAULTS = {"allowed": [], "max_items": 50, "max_age_days": 7}
_ITEM_ID = re.compile(r"c-[0-9a-f]{16}")


class ClipboardStore:
    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "clipboard.json"
        self._images = state_dir / "clipboard"
        self._lock = threading.Lock()
        self._settings: dict[str, Any] = dict(DEFAULTS, allowed=[])
        self._items: list[dict[str, Any]] = []  # newest first
        self._transient: list[dict[str, Any]] = []
        self._load()

    # ------------------------------------------------------------ settings

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return {**self._settings, "allowed": list(self._settings["allowed"])}

    def allowed(self, device_id: str) -> bool:
        with self._lock:
            return device_id in self._settings["allowed"]

    def set_settings(
        self,
        *,
        allowed: list[str] | None = None,
        max_items: int | None = None,
        max_age_days: int | None = None,
    ) -> dict[str, Any]:
        if max_items is not None and not (isinstance(max_items, int) and 1 <= max_items <= 500):
            raise ValueError("Keep between 1 and 500 items.")
        if max_age_days is not None and not (
            isinstance(max_age_days, int) and 0 <= max_age_days <= 365
        ):
            raise ValueError("Keep items for 0 (no limit) to 365 days.")
        if allowed is not None and not all(isinstance(i, str) and valid_id(i) for i in allowed):
            raise ValueError("Unknown device.")
        with self._lock:
            if allowed is not None:
                self._settings["allowed"] = sorted(set(allowed))
            if max_items is not None:
                self._settings["max_items"] = max_items
            if max_age_days is not None:
                self._settings["max_age_days"] = max_age_days
            self._prune()
            self._save()
        return self.settings()

    # --------------------------------------------------------------- items

    def add(
        self,
        *,
        kind: str,
        direction: str,
        device: dict[str, str],
        text: str = "",
        data: bytes = b"",
        mime: str = "",
    ) -> dict[str, Any]:
        """Record an item; returns it (``transient`` when it looks sensitive)."""
        item: dict[str, Any] = {
            "id": f"c-{secrets.token_hex(8)}",
            "kind": kind,
            "direction": direction,
            "device": {"id": str(device.get("id", "")), "name": str(device.get("name", ""))[:60]},
            "at": time.time(),
            "transient": False,
        }
        if kind == "text":
            item["text"] = text
            item["size"] = len(text.encode())
            item["transient"] = looks_sensitive(text) is not None
        else:
            item["mime"] = mime
            item["size"] = len(data)
        with self._lock:
            if item["transient"]:
                self._transient.insert(0, item)
                del self._transient[5:]
            else:
                if kind == "image":
                    self._write_image(item, data)
                self._items.insert(0, item)
                self._prune()
                self._save()
        return dict(item)

    def items(self, query: str = "") -> list[dict[str, Any]]:
        """Newest first: the transient ones (while they last), then the history."""
        now = time.time()
        with self._lock:
            self._transient = [i for i in self._transient if now - i["at"] < TRANSIENT_SECONDS]
            self._prune()
            merged = sorted([*self._transient, *self._items], key=lambda i: i["at"], reverse=True)
        needle = query.strip().lower()
        if needle:
            merged = [
                i
                for i in merged
                if needle in i.get("text", "").lower() or needle in i["device"]["name"].lower()
            ]
        return [dict(i) for i in merged]

    def get(self, item_id: str) -> dict[str, Any] | None:
        return next((i for i in self.items() if i["id"] == item_id), None)

    def image_path(self, item_id: str) -> Path | None:
        item = self.get(item_id)
        if item is None or item["kind"] != "image":
            return None
        path = self._image_file(item)
        return path if path.is_file() else None

    def delete(self, item_id: str) -> bool:
        with self._lock:
            for bucket in (self._items, self._transient):
                for item in bucket:
                    if item["id"] == item_id:
                        bucket.remove(item)
                        self._drop_image(item)
                        self._save()
                        return True
        return False

    def clear(self) -> None:
        with self._lock:
            for item in self._items:
                self._drop_image(item)
            self._items.clear()
            self._transient.clear()
            self._save()

    # ------------------------------------------------------------ internals

    def _image_file(self, item: dict[str, Any]) -> Path:
        return self._images / f"{item['id']}.{IMAGE_MIME.get(item.get('mime', ''), 'bin')}"

    def _write_image(self, item: dict[str, Any], data: bytes) -> None:
        """Readable only by this user, like the rest of the state folder's secrets."""
        self._images.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._image_file(item), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)

    def _drop_image(self, item: dict[str, Any]) -> None:
        if item["kind"] == "image":
            self._image_file(item).unlink(missing_ok=True)

    def _prune(self) -> None:
        days = self._settings["max_age_days"]
        cutoff = time.time() - days * 86400 if days else None
        keep: list[dict[str, Any]] = []
        for item in self._items:
            if len(keep) < self._settings["max_items"] and (cutoff is None or item["at"] >= cutoff):
                keep.append(item)
            else:
                self._drop_image(item)
        self._items = keep

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        settings = data.get("settings") or {}
        try:
            self.set_settings(
                allowed=[
                    i for i in settings.get("allowed", []) if isinstance(i, str) and valid_id(i)
                ],
                max_items=settings.get("max_items", DEFAULTS["max_items"]),
                max_age_days=settings.get("max_age_days", DEFAULTS["max_age_days"]),
            )
        except (ValueError, TypeError):
            log.warning("Ignoring invalid clipboard settings in %s", self._path)
        items = data.get("items")
        if isinstance(items, list):
            self._items = [
                i
                for i in items
                if isinstance(i, dict)
                and {"id", "kind", "at", "device"} <= i.keys()
                and _ITEM_ID.fullmatch(str(i["id"]))
                and not i.get("transient")
            ]
            with self._lock:
                self._prune()

    def _save(self) -> None:
        try:
            write_private(self._path, {"settings": self._settings, "items": self._items})
        except OSError:
            log.warning("Could not save the clipboard history to %s", self._path)
