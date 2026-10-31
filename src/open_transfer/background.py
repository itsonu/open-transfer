"""Keep syncing with no window: the background copy of the desktop app.

``open-transfer-desktop --background`` (the packaged app's own executable with
``--background``) runs the device and the clipboard agent with no window and
no Dock icon. It starts at login when the owner asks for it:

- **macOS:** a LaunchAgent (``~/Library/LaunchAgents``) that launchd restarts
  if it crashes.
- **Windows:** the per-user ``Run`` key.
- **Linux:** an XDG autostart entry.

Opening the app while the background copy runs shows a window onto it; closing
that window leaves the background copy running.
"""

from __future__ import annotations

import contextlib
import logging
import os
import plistlib
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("open_transfer.desktop")

LABEL = "io.github.itsonu.opentransfer.agent"
RUN_VALUE = "Open Transfer"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_command(*args: str) -> list[str]:
    """How to start the desktop app (with ``args``) from outside."""
    if is_frozen():
        return [sys.executable, *args]
    return [sys.executable, "-m", "open_transfer.desktop", *args]


def spawn(*args: str) -> bool:
    """Start another copy of the app, detached from this one (it outlives us)."""
    cmd = app_command(*args)
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if is_frozen():
        # A one-file build unpacks to a temp folder it deletes on exit: the copy must
        # unpack its own instead of sharing ours (PyInstaller 6.9+).
        kwargs["env"] = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kwargs)  # type: ignore[call-overload]  # noqa: S603
    except OSError:
        log.warning("Couldn’t start %s", cmd, exc_info=True)
        return False
    log.info("Started %s", " ".join(cmd))
    return True


@dataclass
class Controls:
    """What the desktop app lets the page control (``/api/app``)."""

    background: bool  # this is the windowless copy
    login: LoginItem | None
    quit: Callable[[], object] | None = None


@dataclass
class LoginItem:
    """Start ``command`` when the owner logs in (one entry per user)."""

    command: list[str]
    platform: str = sys.platform
    home: Path = field(default_factory=Path.home)
    #: The Windows ``Run`` value's name (tests use their own).
    name: str = RUN_VALUE

    @property
    def supported(self) -> bool:
        return self.platform in ("darwin", "win32") or self.platform.startswith("linux")

    # ----------------------------------------------------------------- paths

    @property
    def _plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    @property
    def _desktop_file(self) -> Path:
        config = os.environ.get("XDG_CONFIG_HOME") or str(self.home / ".config")
        return Path(config) / "autostart" / "open-transfer.desktop"

    # ---------------------------------------------------------------- public

    def enabled(self) -> bool:
        if self.platform == "darwin":
            return self._plist.is_file()
        if self.platform == "win32":
            return self._read_run() is not None
        return self._desktop_file.is_file()

    def set(self, on: bool) -> None:
        if self.platform == "darwin":
            self._set_mac(on)
        elif self.platform == "win32":
            self._set_windows(on)
        elif self.platform.startswith("linux"):
            self._set_linux(on)
        else:
            raise OSError("Starting at login isn’t supported on this system.")

    # ------------------------------------------------------------- per OS

    def _set_mac(self, on: bool) -> None:
        if not on:
            self._plist.unlink(missing_ok=True)
            return
        self._plist.parent.mkdir(parents=True, exist_ok=True)
        plist = {
            "Label": LABEL,
            "ProgramArguments": self.command,
            "RunAtLoad": True,
            # Started again by launchd if it crashes, but not after a clean "Quit".
            "KeepAlive": {"SuccessfulExit": False},
            "ProcessType": "Interactive",
            "LimitLoadToSessionType": "Aqua",
        }
        self._plist.write_bytes(plistlib.dumps(plist))

    def _set_linux(self, on: bool) -> None:
        if not on:
            self._desktop_file.unlink(missing_ok=True)
            return
        self._desktop_file.parent.mkdir(parents=True, exist_ok=True)
        self._desktop_file.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Open Transfer\n"
            "Comment=Sync the clipboard with your devices\n"
            f"Exec={shlex.join(self.command)}\n"
            "X-GNOME-Autostart-enabled=true\n"
            "NoDisplay=true\n",
            encoding="utf-8",
        )

    def _read_run(self) -> str | None:
        winreg = _winreg()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _ = winreg.QueryValueEx(key, self.name)
                return str(value)
        except OSError:
            return None

    def _set_windows(self, on: bool) -> None:
        winreg = _winreg()
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if on:
                command = subprocess.list2cmdline(self.command)
                winreg.SetValueEx(key, self.name, 0, winreg.REG_SZ, command)
            else:
                with contextlib.suppress(FileNotFoundError):
                    winreg.DeleteValue(key, self.name)


def _winreg() -> Any:
    import importlib

    return importlib.import_module("winreg")
