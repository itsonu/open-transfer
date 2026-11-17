"""Ask the person at this computer to choose a video, in the system's own dialog.

A browser page never learns where a file it was given lives, so a video can only
be streamed from where it is when the choice is made here, by the system's file
dialog, and only for the owner. The path comes from that dialog, never from a request.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from open_transfer import opener

PROMPT = "Choose a video to watch on another device"
TIMEOUT = 600  # seconds: the dialog stays until the person answers it
WINDOWS = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false;"  # names that aren't ASCII
    "$d = New-Object System.Windows.Forms.OpenFileDialog;"
    f"$d.Title = '{PROMPT}';"
    "$d.Filter = 'Videos|*.mp4;*.m4v;*.mov;*.mkv;*.webm;*.avi;*.ts|All files|*.*';"
    "if ($d.ShowDialog() -eq 'OK') { [Console]::Out.Write($d.FileName) }"
)


def command(platform: str = sys.platform) -> list[str] | None:
    """The program that shows the dialog and prints the chosen path, if there is one."""
    if platform == "darwin":
        script = f'POSIX path of (choose file with prompt "{PROMPT}" of type {{"public.movie"}})'
        return ["osascript", "-e", script]
    if platform == "win32":
        return ["powershell", "-NoProfile", "-STA", "-Command", WINDOWS]
    if shutil.which("zenity"):
        return ["zenity", "--file-selection", f"--title={PROMPT}"]
    if shutil.which("kdialog"):
        return ["kdialog", "--getopenfilename", str(Path.home())]
    return None


def available(platform: str = sys.platform) -> bool:
    """Whether this machine can ask its user to choose a file (not a server or container)."""
    if platform in {"darwin", "win32"}:
        return True
    return opener.available() and command(platform) is not None


def pick_video(platform: str = sys.platform) -> Path | None:
    """The file chosen, or ``None`` when the person closed the dialog."""
    argv = command(platform)
    if argv is None:
        return None
    try:
        done = subprocess.run(  # noqa: S603 - fixed program, no shell
            argv, stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", timeout=TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return None
    chosen = done.stdout.strip().lstrip("\ufeff") if done.returncode == 0 else ""
    return Path(chosen) if chosen else None
