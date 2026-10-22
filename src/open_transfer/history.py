"""What this device sent and received: transfers and clipboard shares.

One JSON object per line in ``history.jsonl`` (private to this user), newest
last, capped. Entries describe what happened, never what was in it: file
names and sizes, who, and how it ended. Clipboard entries say "Text" or
"Image", not the text.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("open_transfer.history")

LIMIT = 1000


class History:
    def __init__(self, state_dir: Path, *, limit: int = LIMIT) -> None:
        self._path = state_dir / "history.jsonl"
        self._limit = limit
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []  # oldest first
        self._load()

    def add(self, entry: dict[str, Any]) -> dict[str, Any]:
        item = {"id": f"h-{secrets.token_hex(8)}", "at": time.time(), **entry}
        with self._lock:
            self._entries.append(item)
            if len(self._entries) > self._limit:
                del self._entries[: len(self._entries) - self._limit]
                self._rewrite()
            else:
                self._append(item)
        return dict(item)

    def latest(self) -> str:
        with self._lock:
            return str(self._entries[-1]["id"]) if self._entries else ""

    def query(
        self,
        *,
        q: str = "",
        device: str = "",
        direction: str = "",
        status: str = "",
        kind: str = "",
        since: float = 0.0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Newest first, matching every filter given."""
        needle = q.strip().lower()
        out: list[dict[str, Any]] = []
        with self._lock:
            entries = list(reversed(self._entries))
        for e in entries:
            devices = e.get("devices") or []
            if direction and e.get("direction") != direction:
                continue
            if status and e.get("status") != status:
                continue
            if kind and e.get("kind") != kind:
                continue
            if since and e.get("at", 0) < since:
                continue
            if device and not any(d.get("id") == device for d in devices):
                continue
            if (
                needle
                and needle not in str(e.get("title", "")).lower()
                and not any(needle in str(d.get("name", "")).lower() for d in devices)
            ):
                continue
            out.append(dict(e))
            if len(out) >= limit:
                break
        return out

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._rewrite()

    # ------------------------------------------------------------ internals

    def _open(self, mode: int) -> int:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(self._path, mode | os.O_WRONLY | os.O_CREAT, 0o600)

    def _append(self, item: dict[str, Any]) -> None:
        try:
            with os.fdopen(self._open(os.O_APPEND), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(item, separators=(",", ":")) + "\n")
        except OSError:
            log.warning("Could not save transfer history to %s", self._path)

    def _rewrite(self) -> None:
        tmp = self._path.with_name(f".{self._path.name}.{secrets.token_hex(4)}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for item in self._entries:
                    fh.write(json.dumps(item, separators=(",", ":")) + "\n")
            os.replace(tmp, self._path)
        except OSError:
            log.warning("Could not save transfer history to %s", self._path)

    def _load(self) -> None:
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue  # a torn or damaged line: skip it, keep the rest
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                self._entries.append(item)
        if len(self._entries) > self._limit:
            del self._entries[: len(self._entries) - self._limit]
            self._rewrite()
