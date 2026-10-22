"""Open a received file with its default app, or show it in its folder.

Only for the person using this device (the owner), on this machine. Programs
and scripts are never opened from here: one click must not run code someone
sent you, so those can only be revealed in their folder.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Anything that runs code when "opened": shown in its folder instead.
UNSAFE_SUFFIXES = frozenset({
    "exe", "msi", "msix", "appx", "appxbundle", "bat", "cmd", "com", "scr", "pif", "cpl", "msc",
    "lnk", "url", "reg", "hta", "vbs", "vbe", "js", "jse", "wsf", "wsh", "ps1", "psm1", "jar",
    "apk", "xapk", "aab", "sh", "bash", "zsh", "csh", "ksh", "command", "tool", "app", "pkg",
    "mpkg", "action", "workflow", "terminal", "desktop", "run", "bin", "appimage", "deb", "rpm",
    "py", "pyw", "pl", "rb",
})  # fmt: skip


def unsafe_to_open(name: str) -> bool:
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return suffix in UNSAFE_SUFFIXES


def command(action: str, path: Path, platform: str = sys.platform) -> list[str]:
    """The program to run for ``action`` ("open" or "reveal") on ``platform``."""
    if platform == "darwin":
        return ["open", "-R", str(path)] if action == "reveal" else ["open", str(path)]
    if platform == "win32":
        # Opening goes through os.startfile (no shell); this is for reveal.
        return ["explorer", "/select,", str(path)]
    # Linux and the rest: no portable "select this file", so open its folder.
    return ["xdg-open", str(path.parent if action == "reveal" else path)]


def available(platform: str = sys.platform) -> bool:
    """Whether this machine can show files to its user (not a server or container)."""
    if platform in {"darwin", "win32"}:
        return True
    graphical = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    return bool(graphical and shutil.which("xdg-open"))


def _run(argv: list[str]) -> None:
    subprocess.Popen(  # noqa: S603 - fixed program, the path is one argument (no shell)
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def open_file(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606 - checked by unsafe_to_open
        return
    _run(command("open", path))


def reveal(path: Path) -> None:
    _run(command("reveal", path))
