"""Transfer history: what was sent or received, with whom, when, and how it ended.

Live transfers are objects in :mod:`open_transfer.mesh`; this module keeps a
durable record of each one in ``<state>/history.db`` (SQLite). The mesh saves a
full snapshot of a transfer on every state change, so the record is always
consistent with what the user last saw, survives restarts, and stays meaningful
after the received files are deleted (a record never owns a file).

One record per transfer and direction: a group send is **one** ``sent`` record
with a result per recipient; each receiver keeps its own ``received`` record
with the same ``transfer_id``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The lifecycle, shared by senders and receivers (see docs/protocol.md).
OFFERED, ACCEPTED, TRANSFERRING = "offered", "accepted", "transferring"
COMPLETED, PARTIAL, FAILED = "completed", "partial", "failed"
DECLINED, EXPIRED, CANCELLED = "declined", "expired", "cancelled"
ACTIVE_STATES = frozenset({OFFERED, ACCEPTED, TRANSFERRING})
FINAL_STATES = frozenset({COMPLETED, PARTIAL, FAILED, DECLINED, EXPIRED, CANCELLED})

#: Live-transfer state names (old wire names included) → lifecycle names.
_STATE_NAMES = {
    "pending": OFFERED,
    "offering": OFFERED,
    "waiting": OFFERED,
    "accepted": ACCEPTED,
    "receiving": TRANSFERRING,
    "sending": TRANSFERRING,
    "done": COMPLETED,
    "canceled": CANCELLED,
}

DEFAULT_MAX_ROWS = 1000
DEFAULT_MAX_AGE = 180 * 24 * 3600.0
MAX_PAGE = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transfers (
  row_id      INTEGER PRIMARY KEY,
  transfer_id TEXT NOT NULL,
  direction   TEXT NOT NULL CHECK (direction IN ('sent', 'received')),
  peer_node   TEXT NOT NULL DEFAULT '',
  sender      TEXT NOT NULL DEFAULT '{}',
  sender_id   TEXT NOT NULL DEFAULT '',
  sender_name TEXT NOT NULL DEFAULT '',
  file_count  INTEGER NOT NULL,
  total_bytes INTEGER NOT NULL,
  state       TEXT NOT NULL,
  reason_code TEXT NOT NULL DEFAULT '',
  reason      TEXT NOT NULL DEFAULT '',
  created_at  REAL NOT NULL,
  started_at  REAL,
  finished_at REAL,
  bytes_done  INTEGER NOT NULL DEFAULT 0,
  unseen      INTEGER NOT NULL DEFAULT 1,
  UNIQUE (transfer_id, direction, peer_node)
);
CREATE TABLE IF NOT EXISTS recipients (
  row_id      INTEGER NOT NULL REFERENCES transfers(row_id) ON DELETE CASCADE,
  device_id   TEXT NOT NULL,
  device      TEXT NOT NULL DEFAULT '{}',
  device_name TEXT NOT NULL DEFAULT '',
  session_id  TEXT NOT NULL DEFAULT '',
  state       TEXT NOT NULL,
  reason_code TEXT NOT NULL DEFAULT '',
  reason      TEXT NOT NULL DEFAULT '',
  bytes_done  INTEGER NOT NULL DEFAULT 0,
  files_done  TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (row_id, device_id)
);
CREATE TABLE IF NOT EXISTS files (
  row_id     INTEGER NOT NULL REFERENCES transfers(row_id) ON DELETE CASCADE,
  idx        INTEGER NOT NULL,
  name       TEXT NOT NULL,
  size       INTEGER NOT NULL,
  mime       TEXT NOT NULL DEFAULT '',
  saved_name TEXT NOT NULL DEFAULT '',
  state      TEXT NOT NULL DEFAULT '',
  error      TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (row_id, idx)
);
CREATE INDEX IF NOT EXISTS transfers_created ON transfers (created_at DESC, row_id DESC);
CREATE INDEX IF NOT EXISTS recipients_device ON recipients (device_id);
"""


def lifecycle_state(state: str) -> str:
    """Map a live state name to its lifecycle name (unknown names pass through)."""
    return _STATE_NAMES.get(state, state)


@dataclass
class RecipientRecord:
    device_id: str
    device: dict[str, Any]  # name, form, platform, kind, via_name
    state: str
    reason_code: str = ""
    reason: str = ""
    session_id: str = ""
    bytes_done: int = 0
    files_done: list[int] = field(default_factory=list)


@dataclass
class FileRecord:
    name: str
    size: int
    mime: str = ""
    saved_name: str = ""
    state: str = ""
    error: str = ""


@dataclass
class TransferRecord:
    transfer_id: str
    direction: str  # "sent" | "received"
    files: list[FileRecord]
    recipients: list[RecipientRecord]
    sender: dict[str, Any]  # who sent it: id, name, form, platform, via_name
    peer_node: str = ""  # received: the sending app's id (sent: "")
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    reason_code: str = ""
    reason: str = ""

    @property
    def state(self) -> str:
        return aggregate_state([r.state for r in self.recipients])


def aggregate_state(states: list[str]) -> str:
    """The state of a whole transfer, from the state of each recipient.

    Rules (docs/protocol.md, "Transfer lifecycle"):
    - any recipient still active -> the earliest active state among them;
    - everyone completed -> completed;
    - at least one completed/partial but not all completed -> partial
      (a group send must never look complete when someone missed out);
    - nobody received anything -> their shared final state, or failed if they differ.
    """
    for active in (OFFERED, ACCEPTED, TRANSFERRING):  # least advanced first
        if active in states:
            return active
    if states and all(s == COMPLETED for s in states):
        return COMPLETED
    if COMPLETED in states or PARTIAL in states:
        return PARTIAL
    return states[0] if len(set(states)) == 1 else FAILED


class History:
    """A SQLite-backed log of transfers. Safe to use from several threads."""

    def __init__(
        self,
        path: Path,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_age: float = DEFAULT_MAX_AGE,
    ) -> None:
        self.path = path
        self.max_rows = max_rows
        self.max_age = max_age
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            # Filenames and device names are private: keep the database owner-only.
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---------------------------------------------------------------- write

    def save(self, record: TransferRecord) -> int:
        """Insert or replace the whole record; returns its ``row_id``."""
        state = record.state
        finished = record.finished_at
        if state in FINAL_STATES and finished is None:
            finished = time.time()
        done = sum(r.bytes_done for r in record.recipients)
        with self._lock, self._db:
            self._db.execute("BEGIN")
            row = self._db.execute(
                "SELECT row_id, state FROM transfers"
                " WHERE transfer_id = ? AND direction = ? AND peer_node = ?",
                (record.transfer_id, record.direction, record.peer_node),
            ).fetchone()
            if row is not None and row["state"] in FINAL_STATES:
                return int(row["row_id"])  # final records never change
            values = (
                json.dumps(record.sender), str(record.sender.get("id") or ""),
                str(record.sender.get("name") or ""), len(record.files),
                sum(f.size for f in record.files), state, record.reason_code,
                record.reason, record.started_at, finished, done,
            )  # fmt: skip
            if row is None:
                cur = self._db.execute(
                    "INSERT INTO transfers (transfer_id, direction, peer_node, sender,"
                    " sender_id, sender_name, file_count, total_bytes, state, reason_code,"
                    " reason, started_at, finished_at, bytes_done, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (record.transfer_id, record.direction, record.peer_node, *values,
                     record.created_at),
                )  # fmt: skip
                row_id = int(cur.lastrowid or 0)
            else:
                row_id = int(row["row_id"])
                self._db.execute(
                    "UPDATE transfers SET sender = ?, sender_id = ?, sender_name = ?,"
                    " file_count = ?, total_bytes = ?,"
                    " state = ?, reason_code = ?, reason = ?, started_at = ?,"
                    " finished_at = ?, bytes_done = ?, unseen = 1 WHERE row_id = ?",
                    (*values, row_id),
                )
                self._db.execute("DELETE FROM recipients WHERE row_id = ?", (row_id,))
                self._db.execute("DELETE FROM files WHERE row_id = ?", (row_id,))
            self._db.executemany(
                "INSERT INTO recipients (row_id, device_id, device, device_name, session_id,"
                " state, reason_code, reason, bytes_done, files_done)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (row_id, r.device_id, json.dumps(r.device), str(r.device.get("name") or ""),
                     r.session_id, r.state,
                     r.reason_code, r.reason, r.bytes_done, json.dumps(sorted(r.files_done)))
                    for r in record.recipients
                ],
            )  # fmt: skip
            self._db.executemany(
                "INSERT INTO files (row_id, idx, name, size, mime, saved_name, state, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (row_id, i, f.name, f.size, f.mime, f.saved_name, f.state, f.error)
                    for i, f in enumerate(record.files)
                ],
            )
        return row_id

    def interrupt_unfinished(self, reason: str = "Open Transfer was closed.") -> int:
        """Mark transfers left active by a previous run as failed; returns how many."""
        with self._lock, self._db:
            self._db.execute("BEGIN")
            rows = [
                int(r["row_id"])
                for r in self._db.execute(
                    "SELECT row_id FROM transfers WHERE state IN (?, ?, ?)", tuple(ACTIVE_STATES)
                )
            ]
            for row_id in rows:
                self._db.execute(
                    "UPDATE recipients SET state = ?, reason_code = 'app_restart', reason = ?"
                    " WHERE row_id = ? AND state IN (?, ?, ?)",
                    (FAILED, reason, row_id, *ACTIVE_STATES),
                )
                states = [
                    str(r["state"])
                    for r in self._db.execute(
                        "SELECT state FROM recipients WHERE row_id = ?", (row_id,)
                    )
                ]
                self._db.execute(
                    "UPDATE transfers SET state = ?, reason_code = 'app_restart', reason = ?,"
                    " finished_at = ?, unseen = 1 WHERE row_id = ?",
                    (aggregate_state(states), reason, time.time(), row_id),
                )
        return len(rows)

    def mark_seen(self, row_ids: list[int]) -> None:
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE transfers SET unseen = 0 WHERE row_id = ?", [(i,) for i in row_ids]
            )

    def delete(self, row_id: int) -> bool:
        """Forget one record. Never touches the files it mentions."""
        with self._lock, self._db:
            cur = self._db.execute(
                "DELETE FROM transfers WHERE row_id = ? AND state NOT IN (?, ?, ?)",
                (row_id, *ACTIVE_STATES),
            )
        return cur.rowcount > 0

    def clear(self, *, completed_only: bool = False) -> int:
        """Forget finished records (all, or only completed ones). Files are kept."""
        states = (COMPLETED,) if completed_only else tuple(FINAL_STATES)
        marks = ", ".join("?" * len(states))
        with self._lock, self._db:
            cur = self._db.execute(f"DELETE FROM transfers WHERE state IN ({marks})", states)  # noqa: S608
        return cur.rowcount

    def prune(self, now: float | None = None) -> int:
        """Apply retention: drop finished records past ``max_age`` or beyond ``max_rows``."""
        now = time.time() if now is None else now
        final = tuple(FINAL_STATES)
        marks = ", ".join("?" * len(final))
        with self._lock, self._db:
            self._db.execute("BEGIN")
            old = self._db.execute(
                f"DELETE FROM transfers WHERE state IN ({marks}) AND created_at < ?",  # noqa: S608
                (*final, now - self.max_age),
            ).rowcount
            extra = self._db.execute(
                f"DELETE FROM transfers WHERE state IN ({marks}) AND row_id NOT IN"  # noqa: S608
                " (SELECT row_id FROM transfers ORDER BY created_at DESC, row_id DESC LIMIT ?)",
                (*final, self.max_rows),
            ).rowcount
        return old + extra

    # ----------------------------------------------------------------- read

    def find(
        self,
        *,
        direction: str | None = None,
        failed: bool = False,
        device: str | None = None,
        text: str | None = None,
        before: tuple[float, int] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Newest first. ``before`` is the ``(created_at, row_id)`` of the last item seen."""
        where: list[str] = []
        args: list[Any] = []
        if direction in {"sent", "received"}:
            where.append("t.direction = ?")
            args.append(direction)
        if failed:
            where.append("t.state IN (?, ?, ?, ?, ?)")
            args += [PARTIAL, FAILED, DECLINED, EXPIRED, CANCELLED]
        if device:
            where.append(
                "(t.sender_id = ? OR t.peer_node = ? OR EXISTS"
                " (SELECT 1 FROM recipients r WHERE r.row_id = t.row_id AND r.device_id = ?))"
            )
            args += [device, device, device]
        if text:
            like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append(
                "(t.sender_name LIKE ? ESCAPE '\\' OR EXISTS"
                " (SELECT 1 FROM files f WHERE f.row_id = t.row_id"
                "  AND (f.name LIKE ? ESCAPE '\\' OR f.saved_name LIKE ? ESCAPE '\\'))"
                " OR EXISTS (SELECT 1 FROM recipients r WHERE r.row_id = t.row_id"
                "  AND r.device_name LIKE ? ESCAPE '\\'))"
            )
            args += [like] * 4
        if before is not None:
            where.append("(t.created_at, t.row_id) < (?, ?)")
            args += list(before)
        sql = "SELECT t.* FROM transfers t"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY t.created_at DESC, t.row_id DESC LIMIT ?"
        args.append(max(1, min(int(limit), MAX_PAGE)))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
            return [self._expand(row) for row in rows]

    def get(self, row_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM transfers WHERE row_id = ?", (row_id,)).fetchone()
            return self._expand(row) if row is not None else None

    def _expand(self, row: sqlite3.Row) -> dict[str, Any]:
        row_id = int(row["row_id"])
        recipients = [
            {
                "device_id": r["device_id"],
                **json.loads(r["device"]),
                "session_id": r["session_id"],
                "state": r["state"],
                "reason_code": r["reason_code"],
                "reason": r["reason"],
                "bytes_done": r["bytes_done"],
                "files_done": json.loads(r["files_done"]),
            }
            for r in self._db.execute(
                "SELECT * FROM recipients WHERE row_id = ? ORDER BY rowid", (row_id,)
            )
        ]
        files = [
            {k: f[k] for k in ("name", "size", "mime", "saved_name", "state", "error")}
            for f in self._db.execute(
                "SELECT * FROM files WHERE row_id = ? ORDER BY idx", (row_id,)
            )
        ]
        return {
            "row_id": row_id,
            "transfer_id": row["transfer_id"],
            "direction": row["direction"],
            "peer_node": row["peer_node"],
            "sender": json.loads(row["sender"]),
            "file_count": row["file_count"],
            "total_bytes": row["total_bytes"],
            "state": row["state"],
            "reason_code": row["reason_code"],
            "reason": row["reason"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "bytes_done": row["bytes_done"],
            "unseen": bool(row["unseen"]),
            "recipients": recipients,
            "files": files,
        }
