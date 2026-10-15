"""Stream a ZIP of several files without building it in memory or on disk."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path

CHUNK_SIZE = 1024 * 1024


class _Sink(io.RawIOBase):
    """A write-only, non-seekable buffer that ``zipfile`` writes into."""

    def __init__(self) -> None:
        super().__init__()
        self._buffer = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, data: bytes) -> int:  # type: ignore[override]
        self._buffer += data
        return len(data)

    def drain(self) -> bytes:
        data = bytes(self._buffer)
        self._buffer.clear()
        return data


def zip_stream(paths: Iterable[Path]) -> Iterator[bytes]:
    """Yield a ZIP archive of ``paths`` chunk by chunk.

    Entries are *stored* (not deflated): shared files are usually photos,
    videos or archives that do not compress, and skipping compression keeps
    "Download all" as fast as the network allows.
    """
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path in paths:
            try:
                # strict_timestamps=False: files dated before 1980 (the ZIP epoch)
                # are clamped instead of aborting a half-sent download.
                info = zipfile.ZipInfo.from_file(path, arcname=path.name, strict_timestamps=False)
                src = path.open("rb")
            except OSError:  # deleted / replaced while the archive was streaming
                continue
            info.compress_type = zipfile.ZIP_STORED
            with src, archive.open(info, "w", force_zip64=True) as dst:
                while True:
                    chunk = src.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    dst.write(chunk)
                    yield sink.drain()
            yield sink.drain()
    yield sink.drain()
