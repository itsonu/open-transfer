"""Build the standalone Open Transfer app in one command.

    python scripts/build_app.py              (python3 on macOS/Linux, py on Windows)
    python scripts/build_app.py --desktop    the desktop app with its own window

Produces an app that needs no Python on the target machine. The default is the
command-line app (it shows the address, QR code and PIN in a terminal):

    dist/open-transfer        macOS / Linux
    dist/open-transfer.exe    Windows

``--desktop`` builds the double-clickable app, which shows Open Transfer in a
native window (WebView2 on Windows, WKWebView on macOS):

    dist/Open Transfer.exe                     Windows (one windowed .exe)
    dist/open-transfer-macos-<arch>.dmg        macOS ("Open Transfer.app", ad-hoc signed;
                                               arch is arm64 or x64)

Build on each OS you want to ship for (PyInstaller doesn't cross-compile).
Uses an isolated ``.build-venv`` so your own environment is left alone, then
smoke-tests the result: the command-line app by starting it and calling
``/api/health``; the desktop app by running it with ``--smoke-test``, which
checks the API and that the UI rendered inside the native window. If no
native window can be created on this machine (no WebView2 runtime, no
display), the desktop check is repeated in browser mode and a warning says
so; ``--require-window`` makes that an error instead.
"""

from __future__ import annotations

import argparse
import os
import platform
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".build-venv"
SPEC = ROOT / "packaging" / "open-transfer.spec"
DESKTOP_SPEC = ROOT / "packaging" / "open-transfer-desktop.spec"
DIST = ROOT / "dist"
WORK = ROOT / "build" / "pyinstaller"
ICONS = ROOT / "build" / "icons"
EXE = DIST / ("open-transfer.exe" if os.name == "nt" else "open-transfer")
APP_NAME = "Open Transfer"
IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"


def say(message: str) -> None:
    print(f"  {message}", flush=True)


def warn(message: str) -> None:
    # Shows up as an annotation on the GitHub Actions run, not just in the log.
    prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") else "  WARNING: "
    print(f"{prefix}{message}", flush=True)


def run(*cmd: str | Path) -> None:
    subprocess.run([str(c) for c in cmd], check=True, cwd=ROOT)  # noqa: S603


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def arch() -> str:
    machine = platform.machine().lower()
    return "arm64" if machine in {"arm64", "aarch64"} else "x64"


def build(spec: Path, packages: list[str]) -> None:
    if sys.version_info < (3, 10):  # noqa: UP036 - friendly message on old Pythons
        sys.exit("Open Transfer needs Python 3.10 or newer to build.")
    if not venv_python().exists():
        say("Creating build environment (.build-venv)…")
        venv.EnvBuilder(with_pip=True, clear=True).create(VENV)
    say(f"Installing {', '.join(packages)}…")
    run(venv_python(), "-m", "pip", "install", "-q", "--disable-pip-version-check",
        "--upgrade", *packages)  # fmt: skip
    if spec == DESKTOP_SPEC:
        say("Making the app icons…")
        run(venv_python(), ROOT / "scripts" / "make_icons.py", "--out", ICONS)
    say("Building the app (takes a minute)…")
    run(venv_python(), "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
        "--distpath", DIST, "--workpath", WORK, spec)  # fmt: skip


# ------------------------------------------------------------------ CLI app


def smoke_test() -> None:
    say("Checking that it starts…")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory() as share:
        proc = subprocess.Popen(  # noqa: S603
            [str(EXE), share, "--host", "127.0.0.1", "--port", str(port),
             "--no-browser", "--no-qr"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )  # fmt: skip
        try:
            deadline = time.time() + 60  # first start unpacks the bundle
            while True:
                try:
                    url = f"http://127.0.0.1:{port}/api/health"
                    with urllib.request.urlopen(url, timeout=2) as res:
                        if res.status == 200:
                            break
                except OSError:
                    if proc.poll() is not None or time.time() > deadline:
                        output = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
                        sys.exit(f"The built app did not start:\n{output}")
                    time.sleep(0.5)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def build_cli() -> Path:
    build(SPEC, [".", "pyinstaller>=6.10"])
    smoke_test()
    return EXE


# -------------------------------------------------------------- desktop app


def run_desktop_smoke(program: Path, *extra: str) -> tuple[bool, str]:
    """Run ``program --smoke-test``; returns (passed, the SMOKE line or what went wrong).

    The windowed app has no console, so the result is read from --smoke-report.
    """
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "smoke.txt"
        cmd = [str(program), "--smoke-test", *extra, "--smoke-report", str(report)]
        try:
            proc = subprocess.run(  # noqa: S603
                cmd, capture_output=True, text=True, errors="replace", timeout=180
            )
            code: int | str = proc.returncode
            output = (proc.stdout + proc.stderr).strip()
        except subprocess.TimeoutExpired as exc:
            code = "timeout"
            output = "".join(
                part.decode(errors="replace") if isinstance(part, bytes) else part
                for part in (exc.stdout or "", exc.stderr or "")
            )
        line = report.read_text(encoding="utf-8").strip() if report.exists() else ""
        log = report.with_suffix(".log")
        details = log.read_text(encoding="utf-8", errors="replace").strip() if log.exists() else ""
    passed = code == 0 and line.startswith("SMOKE OK")
    if not passed:
        if not line:
            line = f"SMOKE FAIL no report (exit {code})"
        for text in (output, details):
            if text:
                line += "\n" + "\n".join(f"      | {row}" for row in text.splitlines()[-40:])
    return passed, line


def desktop_smoke(program: Path, require_window: bool) -> None:
    say("Checking that it starts and shows its window…")
    passed, line = run_desktop_smoke(program)
    say(f"  native window: {line}")
    if passed:
        return
    if " ui=" in line.splitlines()[0] or require_window:
        # The window opened but showed the wrong thing (or a window is required here).
        sys.exit("The desktop app failed its smoke test in a native window.")
    warn(f"The native window could not be verified on this machine: {line.splitlines()[0]}")
    say("Retrying in browser mode (--no-window)…")
    passed, line = run_desktop_smoke(program, "--no-window")
    say(f"  browser mode: {line}")
    if not passed:
        sys.exit("The desktop app failed its smoke test.")
    warn(
        "Desktop app smoke test passed in BROWSER MODE ONLY (--no-window); "
        "the native window was not tested."
    )


def make_dmg(app: Path) -> Path:
    say("Signing (ad hoc) and making the disk image…")
    # Apple silicon refuses to run unsigned code; an ad-hoc signature is enough to
    # start (users still confirm the first launch, as for any app from outside the
    # App Store that isn't notarized).
    run("codesign", "--force", "--deep", "--sign", "-", app)
    run("codesign", "--verify", "--deep", "--strict", app)
    staging = ROOT / "build" / "dmg"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    run("ditto", app, staging / app.name)  # keeps symlinks, permissions and signatures
    (staging / "Applications").symlink_to("/Applications")  # drag-to-install target
    dmg = DIST / f"open-transfer-macos-{arch()}.dmg"
    for attempt in range(1, 4):  # hdiutil is known to fail with "Resource busy" on CI
        try:
            run("hdiutil", "create", "-volname", APP_NAME, "-srcfolder", staging,
                "-ov", "-format", "UDZO", dmg)  # fmt: skip
            break
        except subprocess.CalledProcessError:
            if attempt == 3:
                raise
            say("hdiutil failed; retrying…")
            time.sleep(5 * attempt)
    shutil.rmtree(staging, ignore_errors=True)
    return dmg


def smoke_test_dmg(dmg: Path, require_window: bool) -> None:
    """Run the app straight from the mounted disk image, as a user would."""
    attached = subprocess.run(  # noqa: S603
        ["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-plist", str(dmg)],  # noqa: S607
        check=True,
        capture_output=True,
    )
    mounts = [
        entity["mount-point"]
        for entity in plistlib.loads(attached.stdout).get("system-entities", [])
        if "mount-point" in entity
    ]
    if not mounts:
        sys.exit(f"Could not mount {dmg.name}")
    mount = Path(mounts[0])
    try:
        program = mount / f"{APP_NAME}.app" / "Contents" / "MacOS" / APP_NAME
        if not (mount / "Applications").is_symlink():
            sys.exit("The disk image has no Applications shortcut.")
        desktop_smoke(program, require_window)
    finally:
        for flags in ([], ["-force"]):
            detach = ["hdiutil", "detach", *flags, str(mount)]
            if subprocess.run(detach, check=False).returncode == 0:  # noqa: S603
                break
            time.sleep(2)


def build_desktop(require_window: bool) -> Path:
    build(DESKTOP_SPEC, [".[desktop]", "pyinstaller>=6.10", "pillow>=10"])
    if IS_MAC:
        dmg = make_dmg(DIST / f"{APP_NAME}.app")
        smoke_test_dmg(dmg, require_window)
        return dmg
    program = DIST / (f"{APP_NAME}.exe" if IS_WIN else APP_NAME)
    if not IS_WIN:
        say("Note: on Linux pywebview needs GTK or Qt bindings, or the app uses the browser.")
    desktop_smoke(program, require_window)
    return program


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the standalone Open Transfer app.")
    parser.add_argument(
        "--desktop", action="store_true", help="build the desktop app (native window)"
    )
    parser.add_argument(
        "--require-window",
        action="store_true",
        help="with --desktop: fail if the smoke test can't open a native window",
    )
    parser.add_argument("--keep-build-files", action="store_true", help="keep build/pyinstaller")
    args = parser.parse_args()

    output = build_desktop(args.require_window) if args.desktop else build_cli()
    size = output.stat().st_size / 1_000_000
    say("")
    say(f"Done: {output.relative_to(ROOT)} ({size:.0f} MB)")
    if not args.desktop:
        say("Double-click it, or run it from a terminal — options work like the CLI:")
        say(f"  {EXE.relative_to(ROOT)} --help")
    elif IS_MAC:
        say("Open the .dmg and drag Open Transfer to Applications.")
    else:
        say("Double-click it to start sharing; closing the window stops sharing.")
    if not args.keep_build_files:
        shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
