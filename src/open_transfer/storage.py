"""Filesystem-backed storage for shared files.

Design notes
------------
* Uploads stream straight to ``<root>/.open-transfer/incoming/*.part`` and are
  atomically renamed into place once complete, so half-written files are never
  listed or downloadable.
* Deletes are soft: files move to ``.open-transfer/trash`` and can be restored
  for ``trash_ttl`` seconds (the UI's "Undo"), then they are purged.
* All names coming from clients go through :func:`safe_filename` and every
  resolved path is checked to live inside the storage root.
"""

from __future__ import annotations

import errno
import hashlib
import mimetypes
import os
import re
import secrets
import shutil
import threading
import time
import unicodedata
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

CHUNK_SIZE = 1024 * 1024
STATE_DIR = ".open-transfer"
MAX_NAME_BYTES = 240  # leave headroom below the common 255-byte limit for " (12)"

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}  # fmt: skip
# ":" on Windows would address NTFS alternate data streams ("a.txt:secret").
_PATH_CHARS = "/\\\0:" if os.name == "nt" else "/\\\0"
_UNSAFE_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')

_KINDS = {
    "image": {"png", "jpg", "jpeg", "gif", "webp", "heic", "heif", "bmp", "tiff", "svg", "avif"},
    "video": {"mp4", "mov", "m4v", "webm", "mkv", "avi"},
    "audio": {"mp3", "m4a", "aac", "wav", "flac", "ogg", "opus"},
    "archive": {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "dmg", "iso"},
    "document": {"pdf", "doc", "docx", "odt", "rtf", "txt", "md", "pages", "epub"},
    "spreadsheet": {"xls", "xlsx", "csv", "ods", "numbers"},
    "presentation": {"ppt", "pptx", "odp", "key"},
    "code": {"py", "js", "ts", "json", "html", "css", "sh", "c", "cpp", "go", "rs", "java"},
    "app": {"apk", "exe", "msi", "pkg", "deb", "rpm", "appimage"},
}


class Readable(Protocol):
    """Anything with ``read(size) -> bytes``: a request body, a file, a pipe."""

    def read(self, size: int = -1, /) -> bytes: ...


class StorageError(Exception):
    """Base class for errors that map to a clean HTTP response."""

    status = 400
    code = "storage_error"


class InvalidName(StorageError):
    code = "invalid_name"


class NotFound(StorageError):
    status = 404
    code = "not_found"


class TooLarge(StorageError):
    status = 413
    code = "too_large"


class InsufficientStorage(StorageError):
    status = 507
    code = "insufficient_storage"


class IncompleteUpload(StorageError):
    code = "incomplete_upload"


def safe_filename(name: str) -> str:
    """Turn an untrusted client-supplied name into a safe, portable file name.

    Unlike ``werkzeug.utils.secure_filename`` this keeps Unicode (so "Café.pdf"
    and "照片.jpg" survive) while stripping path components, control and
    reserved characters, leading dots and Windows device names.
    """
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if name.partition(".")[0].rstrip(" ").upper() in _WINDOWS_RESERVED:
        name = f"_{name}"
    if not name:
        raise InvalidName("File name is empty or not allowed.")
    encoded = name.encode("utf-8")
    if len(encoded) > MAX_NAME_BYTES:
        base, ext = os.path.splitext(name)
        ext_bytes = ext.encode("utf-8")[:32]
        keep = MAX_NAME_BYTES - len(ext_bytes)
        base = base.encode("utf-8")[:keep].decode("utf-8", "ignore").rstrip(" .")
        name = base + ext_bytes.decode("utf-8", "ignore")
    return name


def is_listable_name(name: str) -> bool:
    """True for a plain file name that may be listed and served.

    Rejects paths (``/``, ``\\``), NUL, ``.``/``..`` and hidden names — which
    also covers the ``.open-transfer`` state folder.
    """
    return bool(name) and not name.startswith(".") and not any(c in name for c in _PATH_CHARS)


def file_kind(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    for kind, exts in _KINDS.items():
        if ext in exts:
            return kind
    return "other"


@dataclass(frozen=True)
class FileInfo:
    name: str
    size: int
    modified: float
    mime: str
    kind: str

    @classmethod
    def from_path(cls, path: Path) -> FileInfo:
        stat = path.stat()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return cls(path.name, stat.st_size, stat.st_mtime, mime, file_kind(path.name))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Storage:
    def __init__(
        self,
        root: Path,
        *,
        reserve_bytes: int = 0,
        trash_ttl: float = 30.0,
    ) -> None:
        self.root = Path(root).resolve()
        self.reserve_bytes = reserve_bytes
        self.trash_ttl = trash_ttl
        self._state = self.root / STATE_DIR
        self._incoming = self._state / "incoming"
        self._trash = self._state / "trash"
        self._root_str = str(self.root)
        self._root_prefix = os.path.join(self._root_str, "")
        self._lock = threading.Lock()
        for directory in (self.root, self._incoming, self._trash):
            directory.mkdir(parents=True, exist_ok=True)
        self._cleanup_incoming()

    # ------------------------------------------------------------------ reading

    def files(self) -> list[FileInfo]:
        self.purge_trash()
        files: list[FileInfo] = []
        try:
            entries = list(os.scandir(self.root))
        except FileNotFoundError:
            return files
        for entry in entries:
            if not is_listable_name(entry.name) or entry.is_symlink() or not entry.is_file():
                continue
            try:
                files.append(FileInfo.from_path(Path(entry.path)))
            except OSError:  # vanished between scandir and stat
                continue
        files.sort(key=lambda f: f.modified, reverse=True)
        return files

    def fingerprint(self, files: list[FileInfo] | None = None) -> str:
        """A cheap ETag for the file list, so pollers get ``304 Not Modified``."""
        files = self.files() if files is None else files
        digest = hashlib.blake2b(digest_size=12)
        for f in files:
            digest.update(f"{f.name}\0{f.size}\0{f.modified}\n".encode())
        return digest.hexdigest()

    def resolve(self, name: str) -> Path:
        """Return the path of an existing shared file or raise :class:`NotFound`.

        Accepts any name :meth:`files` can list — including ones the owner
        copied in by hand (``Café.pdf`` in NFD, ``what?.txt``) — but never a
        path: no separators, no hidden files, no symlinks, nothing outside
        the shared folder.
        """
        if not is_listable_name(name):
            raise NotFound("File not found.")
        candidate = os.path.normpath(os.path.join(self._root_str, name))
        if not candidate.startswith(self._root_prefix):
            raise NotFound("File not found.")
        path = Path(candidate)
        if path.parent != self.root or path.is_symlink() or not path.is_file():
            raise NotFound("File not found.")
        return path

    def usage(self) -> dict[str, int]:
        total, used, free = shutil.disk_usage(self.root)
        return {"total": total, "used": used, "free": max(0, free - self.reserve_bytes)}

    # ------------------------------------------------------------------ writing

    def save_stream(
        self,
        name: str,
        stream: Readable,
        *,
        length: int | None = None,
        max_size: int = 0,
    ) -> FileInfo:
        """Stream ``stream`` to disk and publish it under a unique name.

        ``length`` is the declared size (``Content-Length``); when given it is
        checked up-front and the received byte count must match it exactly.
        """
        name = safe_filename(name)
        if max_size and length is not None and length > max_size:
            raise TooLarge(f"File is larger than the {_human(max_size)} limit.")
        free = self.usage()["free"]
        if length is not None and length > free:
            raise InsufficientStorage("Not enough free disk space on the receiving computer.")

        part = self._incoming / f"{secrets.token_hex(8)}.part"
        received = 0
        try:
            try:
                with part.open("wb") as out:
                    for chunk in _iter_chunks(stream):
                        received += len(chunk)
                        if max_size and received > max_size:
                            raise TooLarge(f"File is larger than the {_human(max_size)} limit.")
                        if length is None and received > free:
                            raise InsufficientStorage(
                                "Not enough free disk space on the receiving computer."
                            )
                        out.write(chunk)
            except OSError as exc:
                # e.g. several big uploads at once used up the space together
                if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                    raise InsufficientStorage(
                        "Not enough free disk space on the receiving computer."
                    ) from exc
                raise
            if length is not None and received != length:
                raise IncompleteUpload("The upload was interrupted before it finished.")
            return self._publish(part, name)
        finally:
            part.unlink(missing_ok=True)

    def _publish(self, part: Path, name: str) -> FileInfo:
        with self._lock:
            target = self._unique_path(name)
            os.replace(part, target)
        return FileInfo.from_path(target)

    def _unique_path(self, name: str) -> Path:
        candidate = self.root / name
        if not candidate.exists():
            return candidate
        stem, ext = os.path.splitext(name)
        if stem.endswith(".tar"):  # keep "archive (1).tar.gz" instead of "archive.tar (1).gz"
            stem, ext = stem[:-4], ".tar" + ext
        n = 1
        while True:
            candidate = self.root / f"{stem} ({n}){ext}"
            if not candidate.exists():
                return candidate
            n += 1

    # ----------------------------------------------------------- delete / undo

    def delete(self, name: str) -> str:
        """Move a file to the trash and return a token that can restore it."""
        path = self.resolve(name)
        token = secrets.token_urlsafe(12)
        holder = self._trash / token
        holder.mkdir()
        os.replace(path, holder / path.name)
        return token

    def restore(self, token: str) -> FileInfo:
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", token or ""):
            raise NotFound("Nothing to restore.")
        holder = self._trash / token
        items = list(holder.iterdir()) if holder.is_dir() else []
        if not items:
            raise NotFound("This file can no longer be restored.")
        info = self._publish(items[0], items[0].name)
        shutil.rmtree(holder, ignore_errors=True)
        return info

    def purge_trash(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        try:
            holders = list(self._trash.iterdir())
        except FileNotFoundError:
            return
        for holder in holders:
            try:
                if now - holder.stat().st_mtime >= self.trash_ttl:
                    shutil.rmtree(holder, ignore_errors=True)
            except FileNotFoundError:
                continue

    def _cleanup_incoming(self) -> None:
        """Remove partial uploads left behind by a crash or power loss."""
        for part in self._incoming.glob("*.part"):
            part.unlink(missing_ok=True)


def _iter_chunks(stream: Readable) -> Iterator[bytes]:
    while True:
        chunk = stream.read(CHUNK_SIZE)
        if not chunk:
            return
        yield chunk


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.3g} {unit}"
        value /= 1000
    return f"{size} B"  # pragma: no cover
