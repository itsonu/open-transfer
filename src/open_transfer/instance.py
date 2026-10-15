"""One running app per state folder.

Two copies on one state folder would announce the same device id, so the
desktop app and the command line both take :class:`InstanceLock` first. A
second copy finds the first through ``instance.json`` and asks it to come to
the front through ``instance.focus``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from open_transfer import __version__

log = logging.getLogger("open_transfer.instance")

LOCK_FILE = "instance.lock"
INFO_FILE = "instance.json"
FOCUS_FILE = "instance.focus"

# Requests to our own loopback server must never go through an HTTP proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclasses.dataclass(frozen=True)
class RunningInstance:
    """What a running copy of the app wrote to ``instance.json``."""

    pid: int
    port: int

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


_LOCK_HELD_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}


def _try_lock(fh: IO[bytes]) -> bool | None:
    """Lock ``fh`` exclusively without waiting.

    True: we hold the lock. False: another process holds it. None: this
    platform or file system can't lock files (some network drives).
    """
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        return None
    except OSError as exc:
        return False if exc.errno in _LOCK_HELD_ERRNOS else None
    return True


def _unlock(fh: IO[bytes]) -> None:
    with contextlib.suppress(OSError, ImportError):
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def fetch(url: str, timeout: float = 3.0) -> tuple[int, bytes]:
    with _OPENER.open(url, timeout=timeout) as res:
        return int(res.status), res.read()


def fetch_json(url: str, timeout: float = 3.0) -> Any:
    return json.loads(fetch(url, timeout)[1].decode("utf-8"))


def is_alive(instance: RunningInstance) -> bool:
    """Whether an Open Transfer server answers on ``instance``'s port."""
    try:
        return bool(fetch_json(instance.url + "/api/health", 1.5).get("status") == "ok")
    except (OSError, ValueError, AttributeError):
        return False


class InstanceLock:
    """One running app per state folder.

    An exclusive OS lock on ``instance.lock`` (``fcntl.flock`` on macOS and
    Linux, ``msvcrt.locking`` on Windows) is held for as long as the app runs;
    the OS drops it if the app crashes, so it can never go stale.
    ``instance.json`` tells a second copy which port the first one serves on,
    and ``instance.focus`` is how the second copy asks the first to show
    itself.
    """

    def __init__(self, state_dir: Path) -> None:
        self.dir = Path(state_dir)
        self.lock_path = self.dir / LOCK_FILE
        self.info_path = self.dir / INFO_FILE
        self.focus_path = self.dir / FOCUS_FILE
        #: True when the OS couldn't lock the file and we went by ``instance.json`` alone.
        self.advisory = False
        self._fh: IO[bytes] | None = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> bool:
        """Take the lock. False if another running copy holds it."""
        if self._fh is not None:
            return True
        self.dir.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+b")  # noqa: SIM115 - stays open while we hold the lock
        locked = _try_lock(fh)
        if locked is None:
            other = self.read_info()
            if other is not None and other.pid != os.getpid() and is_alive(other):
                fh.close()
                return False
            log.warning("cannot lock %s; relying on %s instead", self.lock_path, INFO_FILE)
            self.advisory = True
        elif not locked:
            fh.close()
            return False
        self._fh = fh
        with contextlib.suppress(OSError):  # a request left over from an earlier run
            self.focus_path.unlink()
        return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        info = self.read_info()
        if info is None or info.pid == os.getpid():
            with contextlib.suppress(OSError):
                self.info_path.unlink()
        _unlock(fh)
        fh.close()

    def write_info(self, port: int) -> None:
        data = {
            "pid": os.getpid(),
            "port": port,
            "url": f"http://127.0.0.1:{port}",
            "version": __version__,
            "started": round(time.time()),
        }
        tmp = self.info_path.with_name(f"{INFO_FILE}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        for attempt in range(10):  # Windows refuses while a second copy is reading it
            try:
                os.replace(tmp, self.info_path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.05)

    def read_info(self) -> RunningInstance | None:
        try:
            data = json.loads(self.info_path.read_text(encoding="utf-8"))
            instance = RunningInstance(pid=int(data["pid"]), port=int(data["port"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return instance if 0 < instance.port < 65536 else None

    def request_focus(self, timeout: float) -> bool:
        """Ask the running copy to show itself. True once it confirms (deletes the request)."""
        try:
            self.focus_path.write_text(secrets.token_hex(8), encoding="utf-8")
        except OSError:
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.focus_path.exists():
                return True
            time.sleep(0.1)
        with contextlib.suppress(OSError):
            self.focus_path.unlink()
        return False


class FocusRequests:
    """Answer "show yourself" requests from copies of the app started later."""

    def __init__(self, path: Path, on_request: Callable[[], object], interval: float = 0.4):
        self.path = path
        self.on_request = on_request
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ot-focus", daemon=True)

    def start(self) -> FocusRequests:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def poll(self) -> bool:
        """Handle a pending request, if any. True if one was handled."""
        if not self.path.exists():
            return False
        try:
            self.on_request()
        except Exception:
            log.warning("could not bring Open Transfer to the front", exc_info=True)
            return False  # no confirmation: the other copy opens the browser instead
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass  # the asking copy gave up and removed it already
        except OSError:  # Windows: still being written; answer on the next tick
            return False
        return True

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.poll()
