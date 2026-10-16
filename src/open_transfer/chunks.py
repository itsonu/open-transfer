"""Resumable chunked uploads for big files.

Small files go in one request (the old path). Files bigger than
``CHUNK_THRESHOLD`` are split by the browser into ``CHUNK_SIZE`` pieces:

    PUT /api/files                    with ``Content-Range: bytes 0-2097151/12345678``
    PUT /api/send/<job>/files/<index> with ``Content-Range`` + ``X-Upload-Id``

Each chunk is written at its offset inside ``data.part`` (sparse, pre-sized
to the total), so chunks may arrive in any order and up to
``PARALLEL_CHUNKS`` may be in flight at once. ``meta.json`` tracks the
received ranges; when every byte arrived the part is atomically published
(shared-folder mode) or streamed to the receivers (device-to-device mode).
A dropped connection only loses the in-flight chunks.

The protocol is a tiny subset of RFC 7233 / RFC 9110:

* ``Content-Range: bytes <start>-<end>/<total>`` on every chunked PUT.
* ``308 Resume Incomplete`` + ``Range: bytes=0-<received-1>`` while incomplete.
* ``201 Created`` when the file is complete.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import TypedDict


class ChunkStatus(TypedDict):
    name: str
    total: int
    received: int
    contiguous: int
    ranges: list[list[int]]


# ------------------------------------------------------------------ tuning

#: Files bigger than this should use the chunked path (the browser decides).
CHUNK_THRESHOLD = 4 * 1024 * 1024
#: Size the browser splits big files into. 2 MiB keeps each request fast on a
#: LAN, small enough to retry cheaply, big enough to avoid request overhead.
CHUNK_SIZE = 2 * 1024 * 1024
#: How many chunks the browser may upload at once (browser -> this app).
PARALLEL_CHUNKS = 3
#: Hard cap for a single chunk request (protects the server's memory).
MAX_CHUNK_BYTES = 8 * 1024 * 1024
#: Abandoned chunk sessions are cleaned up after this long.
SESSION_TTL = 24 * 3600.0

_lock = threading.RLock()


def parse_content_range(value: str | None) -> tuple[int, int, int] | None:
    """Parse ``bytes 0-99/1234`` → ``(0, 99, 1234)``. ``None`` when absent/invalid."""
    if not value:
        return None
    text = value.strip()
    if not text.lower().startswith("bytes "):
        return None
    text = text[6:].strip()
    try:
        range_part, _, total_part = text.partition("/")
        start_part, _, end_part = range_part.partition("-")
        start, end, total = int(start_part), int(end_part), int(total_part)
    except ValueError:
        return None
    if start < 0 or end < start or total <= 0 or end >= total or start >= total:
        return None
    return start, end, total


def content_range_header(start: int, end: int, total: int) -> str:
    return f"bytes {start}-{end}/{total}"


def merge_ranges(ranges: list[list[int]]) -> list[list[int]]:
    """Merge overlapping ``[start, end)`` intervals, sorted."""
    cleaned = sorted((int(s), int(e)) for s, e in ranges if int(e) > int(s))
    merged: list[list[int]] = []
    for start, end in cleaned:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def missing_ranges(
    total: int, received: list[list[int]], chunk_size: int = CHUNK_SIZE
) -> list[tuple[int, int]]:
    """Chunk-aligned ``(start, end)`` spans not yet fully covered by ``received``.

    ``end`` is exclusive. The last span may be shorter than ``chunk_size``.
    """
    covered = merge_ranges(received)
    missing: list[tuple[int, int]] = []
    offset = 0
    index = 0
    while offset < total:
        end = min(offset + chunk_size, total)
        fully = any(s <= offset and e >= end for s, e in covered)
        if not fully:
            missing.append((offset, end))
        _ = index
        index += 1
        offset = end
    return missing


class ChunkSessions:
    """Assembles chunked uploads on disk. Thread-safe, crash-safe.

    Layout per session ``<base>/<upload_id>/``::

        data.part  — sparse file pre-sized to the total; chunks written at offsets
        meta.json  — {"name": ..., "total": ..., "ranges": [[s, e], ...], "updated": ...}
    """

    def __init__(self, base: Path) -> None:
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- lifecycle

    def _dir(self, upload_id: str) -> Path:
        if not upload_id or "/" in upload_id or "\\" in upload_id or upload_id.startswith("."):
            raise ValueError("bad upload id")
        # ids we generate are urlsafe; still whitelist the charset.
        if not all(c.isalnum() or c in "-_" for c in upload_id):
            raise ValueError("bad upload id")
        return self.base / upload_id

    def _read_meta(self, directory: Path) -> dict[str, object] | None:
        try:
            return json.loads((directory / "meta.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]
        except (OSError, ValueError):
            return None

    def _write_meta(self, directory: Path, name: str, total: int, ranges: list[list[int]]) -> None:
        meta = {"name": name, "total": total, "ranges": ranges, "updated": time.time()}
        (directory / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    def begin(self, name: str, total: int) -> str:
        if total <= 0:
            raise ValueError("total must be positive")
        upload_id = secrets.token_urlsafe(12)
        directory = self._dir(upload_id)
        directory.mkdir(parents=True, exist_ok=False)
        part = directory / "data.part"
        part.touch()
        try:
            with part.open("r+b") as fh:
                fh.truncate(total)
        except OSError:
            pass
        self._write_meta(directory, name, total, [])
        return upload_id

    def status(self, upload_id: str) -> ChunkStatus | None:
        try:
            directory = self._dir(upload_id)
        except ValueError:
            return None
        meta_path = directory / "meta.json"
        part_path = directory / "data.part"
        if not meta_path.is_file() or not part_path.is_file():
            return None
        meta = self._read_meta(directory)
        if meta is None:
            return None
        try:
            total = int(str(meta.get("total", 0)))
        except (TypeError, ValueError):
            return None
        raw_ranges = meta.get("ranges")
        if isinstance(raw_ranges, list):
            parsed: list[list[int]] = []
            for item in raw_ranges:
                if isinstance(item, list) and len(item) == 2:
                    try:
                        parsed.append([int(str(item[0])), int(str(item[1]))])
                    except (TypeError, ValueError):
                        continue
            ranges = merge_ranges(parsed)
        else:
            # Sessions written by the older sequential build stored only a
            # "received" byte count; those were always contiguous from zero.
            try:
                received_legacy = int(str(meta.get("received", 0)))
            except (TypeError, ValueError):
                received_legacy = 0
            ranges = [[0, received_legacy]] if received_legacy > 0 else []
        received = sum(e - s for s, e in ranges)
        contiguous = 0
        for start, end in ranges:
            if start <= contiguous:
                contiguous = max(contiguous, end)
            else:
                break
        return ChunkStatus(
            name=str(meta.get("name", "")),
            total=total,
            received=received,
            contiguous=contiguous,
            ranges=ranges,
        )

    def append(self, upload_id: str, offset: int, data: bytes) -> int:
        """Write ``data`` at ``offset`` (any order; rewrites are idempotent)."""
        with _lock:
            info = self.status(upload_id)
            if info is None:
                raise KeyError("unknown upload")
            total = info["total"]
            name = info["name"]
            ranges = [list(r) for r in info["ranges"]]
            if offset < 0 or offset + len(data) > total:
                raise ValueError("chunk goes past the end of the file")
            if not data:
                return info["received"]
            directory = self._dir(upload_id)
            part = directory / "data.part"
            try:
                with part.open("r+b") as out:
                    out.seek(offset)
                    out.write(data)
            except OSError as exc:
                raise ValueError("could not store chunk") from exc
            ranges = merge_ranges([*ranges, [offset, offset + len(data)]])
            self._write_meta(directory, name, total, ranges)
            return sum(e - s for s, e in ranges)

    def complete(self, upload_id: str) -> bool:
        info = self.status(upload_id)
        if info is None:
            return False
        return info["received"] >= info["total"] and info["total"] > 0

    def path(self, upload_id: str) -> Path | None:
        try:
            candidate = self._dir(upload_id) / "data.part"
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def discard(self, upload_id: str) -> None:
        import shutil

        try:
            directory = self._dir(upload_id)
        except ValueError:
            return
        shutil.rmtree(directory, ignore_errors=True)

    def touch(self, upload_id: str, name: str, total: int, received: int) -> None:
        """Create/refresh a session dir when the first chunk carries its own id.

        Lets the browser pick the upload id up-front (``X-Upload-Id``) so a
        page reload can resume with the same id without an extra round-trip.
        """
        with _lock:
            directory = self._dir(upload_id)
            directory.mkdir(parents=True, exist_ok=True)
            part = directory / "data.part"
            if not part.is_file():
                part.touch()
            try:
                if part.stat().st_size != total and total > 0:
                    with part.open("r+b") as fh:
                        fh.truncate(total)
            except OSError:
                pass
            meta_path = directory / "meta.json"
            if meta_path.is_file():
                # Adopt legacy sequential sessions instead of resetting them.
                existing = self._read_meta(directory)
                if existing is not None and "ranges" not in existing:
                    try:
                        legacy = int(str(existing.get("received", received)))
                    except (TypeError, ValueError):
                        legacy = received
                    self._write_meta(
                        directory,
                        str(existing.get("name", name)),
                        total,
                        [[0, legacy]] if legacy else [],
                    )
                return
            ranges = [[0, received]] if received > 0 else []
            self._write_meta(directory, name, total, ranges)

    def cleanup(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        removed = 0
        try:
            entries = list(self.base.iterdir())
        except FileNotFoundError:
            return 0
        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
                updated = float(meta.get("updated", 0))
            except (OSError, ValueError):
                updated = 0.0
            if now - updated > SESSION_TTL:
                import shutil

                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        return removed
