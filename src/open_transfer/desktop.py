"""Open Transfer as a double-clickable desktop app.

The app runs the same :class:`~open_transfer.node.Node` as the command line
(HTTP server and nearby-device discovery on background threads) and shows its
web UI in a native window: WebView2 on Windows, WKWebView on macOS, through
`pywebview <https://pywebview.flowrl.com>`_. Whoever connects from 127.0.0.1
is the device's owner, so the window simply loads ``node.local_url``.

    python -m open_transfer.desktop [folder] [CLI options] [--no-window] [--smoke-test]

* Closing the window stops sharing and quits.
* Only one copy runs per state folder (two would announce the same device id).
  Starting another one brings the running copy's window to the front, or
  opens its page in the browser, and exits.
* Without pywebview, or when the system web view can't start (no WebView2
  runtime, for example), the UI opens in the default browser and a small
  native dialog offers to stop sharing.
* ``--smoke-test`` starts everything on a scratch folder, checks the API and
  that the UI rendered inside the native web view, prints one line,
  ``SMOKE OK …`` or ``SMOKE FAIL …``, and exits 0 or 1 within a minute.
  The build script runs it on every packaged app.

A windowed app has no console, so logs go to ``<state folder>/open-transfer.log``.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import html
import importlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from open_transfer import __version__
from open_transfer.cli import build_parser, config_from_args, is_frozen_app
from open_transfer.config import Config, env, env_bool
from open_transfer.instance import (
    FocusRequests,
    InstanceLock,
    fetch,
    fetch_json,
    is_alive,
)

if TYPE_CHECKING:
    from open_transfer.node import Node

log = logging.getLogger("open_transfer.desktop")

APP_NAME = "Open Transfer"
LOG_FILE = "open-transfer.log"
WINDOW_SIZE = (1120, 800)
MIN_SIZE = (380, 560)
#: The native page must finish loading within this many seconds, or the app
#: falls back to the browser (a blank WebView2 window helps nobody).
LOAD_TIMEOUT = 45.0
#: --smoke-test always exits within this many seconds.
SMOKE_TIMEOUT = 60.0
SMOKE_LOAD_TIMEOUT = 40.0
#: Proves the Open Transfer page rendered inside the native web view.
UI_PROBE = "document.querySelector('.brand-name') ? 'ok' : 'missing'"
#: Smoke-test failures that mean "no native window on this machine" rather
#: than "the app is broken" start with this; the build retries with --no-window.
WINDOW_UNAVAILABLE = "window-unavailable"

# Windows MessageBox flags.
_MB_ICONERROR = 0x10
_MB_ICONINFORMATION = 0x40
_MB_SETFOREGROUND = 0x10000


# ===================================================================== arguments


def build_desktop_parser() -> argparse.ArgumentParser:
    parser = build_parser()
    parser.prog = "open-transfer-desktop"
    parser.description = "Open Transfer in its own window: AirDrop for every device."
    group = parser.add_argument_group("desktop app")
    group.add_argument(
        "--no-window",
        action="store_true",
        help="use the default web browser instead of a native window",
    )
    group.add_argument(
        "--smoke-test",
        action="store_true",
        help="start on a scratch folder, check that the UI loads, print SMOKE OK or "
        "SMOKE FAIL and exit (0 = OK)",
    )
    group.add_argument(
        "--smoke-report",
        type=Path,
        default=None,
        metavar="PATH",
        help="also write the smoke-test result to PATH (a windowed app has no console)",
    )
    return parser


def clean_argv(argv: Sequence[str]) -> list[str]:
    """Drop the ``-psn_0_12345`` process serial number macOS may add when Finder launches us."""
    return [arg for arg in argv if not arg.startswith("-psn_")]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    raw = sys.argv[1:] if argv is None else argv
    return build_desktop_parser().parse_args(clean_argv(raw))


def smoke_config(args: argparse.Namespace, scratch: Path) -> Config:
    """The configuration for --smoke-test: a throwaway device on a free loopback port.

    The device identity always lives in ``scratch`` so a smoke test never
    touches (or locks) the real app's state. Files go to ``scratch`` unless a
    folder was given.
    """
    config = config_from_args(args)
    changes: dict[str, Any] = {"state_dir": scratch / "state"}
    if args.directory is None and env("DIR") is None:
        changes["storage_dir"] = scratch / "files"
    if args.port is None and env("PORT") is None:
        changes["port"] = 0
    if args.host is None and env("HOST") is None:
        changes["host"] = "127.0.0.1"
    return dataclasses.replace(config, **changes)


def hand_off(
    lock: InstanceLock,
    *,
    wait: float = 15.0,
    ack_timeout: float = 4.0,
    open_url: Callable[[str], object] | None = None,
) -> int:
    """Another copy owns this state folder: bring it forward, then exit."""
    deadline = time.monotonic() + wait
    while True:  # it may still be starting up
        other = lock.read_info()
        if other is not None and is_alive(other):
            break
        if time.monotonic() >= deadline:
            return _fatal(
                "Open Transfer is already running but isn't responding.\n\n"
                "Quit it (or restart your computer) and try again."
            )
        time.sleep(0.3)
    _allow_foreground(other.pid)
    if lock.request_focus(ack_timeout):
        log.info("Open Transfer is already running; asked it to come to the front")
        return 0
    log.info("Open Transfer is already running at %s; opening it in the browser", other.url)
    (open_url or webbrowser.open)(other.url)
    return 0


def _allow_foreground(pid: int) -> None:
    """Windows only lets the foreground app raise windows; let the running copy raise its own."""
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.user32.AllowSetForegroundWindow(pid)


# ======================================================================= dialogs


def _message_box(text: str, *, error: bool = False) -> bool:
    """A blocking Windows message box. False if it couldn't be shown."""
    if sys.platform == "win32":
        try:
            import ctypes

            icon = _MB_ICONERROR if error else _MB_ICONINFORMATION
            ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, icon | _MB_SETFOREGROUND)
        except Exception:
            log.debug("MessageBoxW failed", exc_info=True)
        else:
            return True
    return False


def _osascript(*lines: str, args: Sequence[str] = ()) -> subprocess.CompletedProcess[str] | None:
    """Run AppleScript on macOS; ``args`` arrive as ``argv`` (no quoting to get wrong)."""
    if sys.platform == "darwin":
        cmd = [shutil.which("osascript") or "/usr/bin/osascript"]
        for line in ["on run argv", *lines, "end run"]:
            cmd += ["-e", line]
        try:
            return subprocess.run([*cmd, *args], capture_output=True, text=True, check=False)  # noqa: S603
        except OSError:
            log.debug("osascript failed", exc_info=True)
    return None


def _fatal(message: str, code: int = 1) -> int:
    """Report an error that stops the app: log, stderr and, for the packaged app, a dialog."""
    log.error("%s", message)
    with contextlib.suppress(Exception):
        print(f"open-transfer: error: {message}", file=sys.stderr)
    # Only the packaged app shows a dialog: from a terminal or the test suite it would block.
    if is_frozen_app() and not _message_box(message, error=True):
        _osascript(
            "display alert (item 1 of argv) message (item 2 of argv) as critical",
            args=[f"{APP_NAME} can't start", message],
        )
    return code


def wait_until_quit(url: str, folder: Path, open_url: Callable[[str], object]) -> None:
    """Browser mode: block until the user chooses to stop sharing."""
    message = (
        f"Open Transfer is running in your web browser at {url}\n\n"
        "Nearby devices can find this computer and send it files while it runs. "
        f"Received files are saved to {folder}.\n\n"
    )
    windows_tip = "Closed the page? Start Open Transfer again to reopen it.\n\n"
    if _message_box(message + windows_tip + "Click OK to stop sharing."):
        return
    if sys.platform == "darwin" and _mac_running_dialog(message, url, open_url):
        return
    _wait_for_interrupt()


def _mac_running_dialog(message: str, url: str, open_url: Callable[[str], object]) -> bool:
    failures = 0
    while failures < 3:
        started = time.monotonic()
        result = _osascript(
            "display dialog (item 1 of argv) with title (item 2 of argv) buttons "
            '{"Open in Browser", "Stop Sharing"} default button "Open in Browser" with icon note',
            args=[message + "Choose Stop Sharing to quit.", APP_NAME],
        )
        if result is None:
            return False
        if result.returncode == 0:
            if "Open in Browser" in result.stdout:
                open_url(url)
                continue
            return True
        if time.monotonic() - started < 2:  # failing straight away: no GUI session
            failures += 1
        log.debug("dialog failed: %s", result.stderr.strip())
    return False


def _wait_for_interrupt() -> None:
    import signal

    stop = threading.Event()
    with contextlib.suppress(ValueError, OSError):  # not on the main thread
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    log.info("Sharing. Press Ctrl+C to stop.")
    with contextlib.suppress(KeyboardInterrupt):
        while not stop.wait(0.5):
            pass


# ======================================================================= logging


def _real_stream(stream: Any) -> bool:
    return stream is not None and getattr(stream, "name", "") != os.devnull


def _ensure_std_streams() -> None:
    """A windowed app (PyInstaller ``console=False``) has no stdout/stderr at all."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")  # noqa: SIM115


@contextlib.contextmanager
def logging_to(log_file: Path | None, verbose: bool) -> Iterator[Path | None]:
    """Log to a rotating file (1 MB x 3) and, when there is one, the console."""
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    handlers: list[logging.Handler] = []
    written: Path | None = None
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                logging.handlers.RotatingFileHandler(
                    log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
                )
            )
            written = log_file
        except OSError:
            pass  # read-only or missing folder: log to stderr only
    if _real_stream(sys.stderr):
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(formatter)
    saved = []
    for name in ("open_transfer", "pywebview"):
        logger = logging.getLogger(name)
        saved.append((logger, logger.handlers[:], logger.level, logger.propagate))
        logger.handlers[:] = handlers
        logger.setLevel(logging.DEBUG if verbose else logging.INFO)
        logger.propagate = False
    try:
        yield written
    finally:
        for logger, old_handlers, level, propagate in saved:
            logger.handlers[:] = old_handlers
            logger.setLevel(level)
            logger.propagate = propagate
        for handler in handlers:
            if isinstance(handler, logging.FileHandler):
                handler.close()


# ================================================================ native window


def load_webview() -> tuple[Any, str]:
    """Import pywebview. Returns ``(module, "")`` or ``(None, why it failed)``."""
    try:
        return importlib.import_module("webview"), ""
    except Exception as exc:  # ImportError, or a broken install
        return None, f"{type(exc).__name__}: {exc}"


def webview_storage_dir() -> Path | None:
    """Where WebView2 keeps its profile on Windows (pywebview's default is a shared folder)."""
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / APP_NAME / "WebView"
    return None


def _subscribe(events: Any, name: str, handler: Callable[..., Any]) -> None:
    event = getattr(events, name, None)  # older pywebview versions have fewer events
    if event is not None:
        event += handler


class WindowApp:
    """The pywebview window showing the node's UI.

    :meth:`run` blocks on the main thread until the window closes. It returns
    False, with :attr:`error` saying why, when the window couldn't be shown
    properly; the caller then falls back to the browser.
    """

    def __init__(
        self,
        webview: Any,
        url: str,
        *,
        debug: bool = False,
        private: bool = False,
        storage: Path | None = None,
        load_timeout: float = LOAD_TIMEOUT,
    ) -> None:
        self.webview = webview
        self.url = url
        self.debug = debug
        self.private = private
        self.storage = storage
        self.load_timeout = load_timeout
        self.window: Any = None
        self.renderer: str | None = None
        self.error: str | None = None
        self.loaded = threading.Event()
        self.closed = threading.Event()
        self.minimized = False
        #: Called (on the GUI thread) when the user closes the window or quits the app.
        self.on_closing: Callable[[], object] | None = None
        #: Called on a background thread once the GUI loop is starting.
        self.on_ready: Callable[[WindowApp], object] | None = None

    # ------------------------------------------------------------- control

    def fail(self, reason: str) -> None:
        if self.error is None:
            self.error = reason
            log.error("native window: %s", reason)
        self.close()

    def close(self) -> None:
        if self.window is not None and not self.closed.is_set():
            with contextlib.suppress(Exception):
                self.window.destroy()

    def bring_to_front(self) -> None:
        if self.window is None or self.closed.is_set():
            raise RuntimeError("the window is not open")
        if self.minimized:
            self.window.restore()
        self.window.show()

    def evaluate(self, script: str, timeout: float) -> tuple[Any, str]:
        """Run a JavaScript expression in the page; returns ``(value, how)``.

        ``window.evaluate_js`` wraps the script in ``eval()``, which the app's
        Content-Security-Policy (``script-src 'self'``, no ``'unsafe-eval'``)
        rightly forbids in WebView2 and WKWebView. ``window.run_js`` hands the
        script to the web view as is (like pywebview's own bridge), which a
        page's CSP doesn't restrict, and returns its value on Windows and macOS.
        So: ``evaluate_js`` where the page allows it, else ``run_js``.
        """
        try:
            return _with_timeout(lambda: self.window.evaluate_js(script), timeout), "evaluate_js"
        except Exception as exc:
            if not _blocked_eval(exc) or not hasattr(self.window, "run_js"):
                raise
        return _with_timeout(lambda: self.window.run_js(script), timeout), "run_js"

    # ----------------------------------------------------------------- run

    def run(self) -> bool:
        wv = self.webview
        _enable_downloads(wv)
        try:
            window = wv.create_window(
                APP_NAME,
                self.url,
                width=WINDOW_SIZE[0],
                height=WINDOW_SIZE[1],
                min_size=MIN_SIZE,
                text_select=True,
            )
        except Exception as exc:
            self.error = f"could not create the window: {type(exc).__name__}: {exc}"
            return False
        if window is None:
            self.error = "could not create the window"
            return False
        self.window = window
        events = window.events
        _subscribe(events, "initialized", self._on_initialized)
        _subscribe(events, "loaded", self._on_loaded)
        _subscribe(events, "closing", self._on_closing)
        _subscribe(events, "closed", self.closed.set)
        _subscribe(events, "minimized", self._on_minimized)
        _subscribe(events, "restored", self._on_restored)
        _subscribe(events, "maximized", self._on_restored)
        threading.Thread(target=self._watchdog, name="ot-window-watchdog", daemon=True).start()

        options: dict[str, Any] = {"private_mode": self.private, "debug": self.debug}
        if sys.platform == "win32":
            options["gui"] = "edgechromium"
        if self.storage is not None and not self.private:
            options["storage_path"] = str(self.storage)
        try:
            wv.start(self._started, **options)
        except Exception as exc:
            log.debug("webview.start failed", exc_info=True)
            self.error = self.error or f"the web view could not start: {type(exc).__name__}: {exc}"
        finally:
            self.closed.set()
        shown = getattr(getattr(events, "shown", None), "is_set", lambda: True)()
        if self.error is None and not shown:
            self.error = "the window never appeared"
        return self.error is None

    # -------------------------------------------------------------- events

    def _started(self) -> None:
        if self.on_ready is not None:
            self.on_ready(self)

    def _on_initialized(self, renderer: str | None = None) -> bool:
        # pywebview passes the renderer it picked: "edgechromium", "mshtml", "cocoa"…
        self.renderer = renderer or getattr(self.webview, "renderer", None)
        if self.renderer == "mshtml":
            # pywebview falls back to Internet Explorer when WebView2 is missing; our UI
            # needs a modern engine, so cancel the window and use the browser instead.
            self.error = "the Microsoft Edge WebView2 Runtime is not installed"
            log.error("native window: %s", self.error)
            return False
        return True

    def _on_loaded(self) -> None:
        if self.renderer is None:
            self.renderer = getattr(self.webview, "renderer", None)
        if self.renderer == "mshtml":
            self.fail("the Microsoft Edge WebView2 Runtime is not installed")
            return
        self.loaded.set()

    def _on_closing(self) -> None:
        # Return None: returning False would cancel the close.
        if self.error is None and self.on_closing is not None:
            self.on_closing()

    def _on_minimized(self) -> None:
        self.minimized = True

    def _on_restored(self) -> None:
        self.minimized = False

    def _watchdog(self) -> None:
        if not self.loaded.wait(self.load_timeout) and not self.closed.is_set():
            self.fail(f"the page did not load within {self.load_timeout:.0f} s")


def _with_timeout(call: Callable[[], Any], timeout: float) -> Any:
    """``call()`` on a helper thread: pywebview's JavaScript calls can wait forever."""
    result: list[Any] = []
    errors: list[Exception] = []

    def run() -> None:
        try:
            result.append(call())
        except Exception as exc:  # re-raised on the caller's thread
            errors.append(exc)

    thread = threading.Thread(target=run, name="ot-evaluate", daemon=True)
    thread.start()
    thread.join(timeout)
    if errors:
        raise errors[0]
    if not result:
        raise TimeoutError(f"JavaScript did not return within {timeout:.0f} s")
    return result[0]


def _blocked_eval(exc: BaseException) -> bool:
    """Whether ``exc`` is the page's CSP refusing ``eval()``."""
    text = repr(exc.args) + str(exc)
    return "unsafe-eval" in text or "EvalError" in text


def _enable_downloads(webview: Any) -> None:
    settings = getattr(webview, "settings", None)
    with contextlib.suppress(Exception):
        if settings is not None and "ALLOW_DOWNLOADS" in settings:
            settings["ALLOW_DOWNLOADS"] = True


class _Stopper:
    """Stop the node once, off the GUI thread, waiting a bounded time."""

    def __init__(self, node: Node) -> None:
        self._node = node
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> threading.Thread:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._node.stop, name="ot-stop", daemon=True)
                self._thread.start()
            return self._thread

    def stop(self, wait: float) -> None:
        self.start().join(wait)


# ===================================================================== the app


def main(argv: Sequence[str] | None = None) -> int:
    _ensure_std_streams()
    args = parse_args(argv)
    if args.smoke_test:
        return _smoke_main(args)
    try:
        config = config_from_args(args)
    except ValueError as exc:
        return _fatal(str(exc), code=2)
    lock = InstanceLock(config.state_path)
    try:
        acquired = lock.acquire()
    except OSError as exc:
        return _fatal(f"Open Transfer can't use {config.state_path}:\n{exc}")
    if not acquired:
        # Only the copy holding the lock writes the log file.
        with logging_to(None, args.verbose):
            return hand_off(lock)
    try:
        with logging_to(config.state_path / LOG_FILE, args.verbose) as log_file:
            log.info("Open Transfer %s starting (log: %s)", __version__, log_file)
            try:
                return _serve(args, config, lock)
            except Exception as exc:
                log.exception("Open Transfer stopped unexpectedly")
                return _fatal(f"Open Transfer stopped unexpectedly: {exc}\n\nDetails: {log_file}")
    finally:
        lock.release()


def _start_node(config: Config) -> Node:
    from open_transfer.node import Node

    try:
        node = Node(config)
    except OSError as exc:
        raise _StartError(
            f"Open Transfer can't use the folder {config.storage_dir}:\n{exc}"
        ) from exc
    try:
        node.start()
    except OSError as exc:
        node.stop()
        raise _StartError(f"Open Transfer couldn't start sharing: {exc}") from exc
    return node


class _StartError(Exception):
    pass


def _serve(args: argparse.Namespace, config: Config, lock: InstanceLock) -> int:
    try:
        node = _start_node(config)
    except _StartError as exc:
        return _fatal(str(exc))
    url = node.local_url
    log.info(
        "sharing as %r on port %d; files go to %s",
        node.mesh.identity.name,
        node.port,
        config.storage_dir,
    )
    try:
        lock.write_info(node.port)
    except OSError:
        log.warning("could not write %s", lock.info_path, exc_info=True)
    stopper = _Stopper(node)
    try:
        if not args.no_window:
            webview, why = load_webview()
            if webview is None:
                log.warning("pywebview is not available (%s); using the web browser", why)
            else:
                app = WindowApp(webview, url, debug=args.verbose, storage=webview_storage_dir())

                def closing() -> None:
                    # Also runs when Cmd+Q terminates the process without returning from
                    # webview.start(), so stop here (bounded: never hang the GUI).
                    stopper.stop(wait=1.0)

                app.on_closing = closing
                watcher = FocusRequests(lock.focus_path, app.bring_to_front).start()
                try:
                    if app.run():
                        return 0
                finally:
                    watcher.stop()
                log.warning("native window unavailable (%s); using the web browser", app.error)

        def open_url(target: str) -> None:
            webbrowser.open(target)

        watcher = FocusRequests(lock.focus_path, lambda: open_url(url)).start()
        try:
            if not (args.no_browser or env_bool("NO_BROWSER")):
                open_url(url)
            wait_until_quit(url, config.storage_dir, open_url)
        finally:
            watcher.stop()
        return 0
    finally:
        stopper.stop(wait=5.0)
        log.info("stopped sharing")


# =================================================================== smoke test


class SmokeFailure(Exception):
    pass


class SmokeReport:
    """The one ``SMOKE OK …`` / ``SMOKE FAIL …`` line. The first result wins."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.line: str | None = None
        self._lock = threading.Lock()

    @property
    def done(self) -> bool:
        return self.line is not None

    @property
    def passed(self) -> bool:
        return bool(self.line and self.line.startswith("SMOKE OK"))

    def ok(self, details: str) -> None:
        self._finish(f"SMOKE OK {details}")

    def fail(self, reason: str) -> None:
        self._finish(f"SMOKE FAIL {reason}")

    def _finish(self, line: str) -> None:
        line = " ".join(line.split())  # always a single line
        with self._lock:
            if self.line is not None:
                return
            self.line = line
        with contextlib.suppress(Exception):
            print(line, flush=True)
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(line + "\n", encoding="utf-8")
            except OSError:
                log.exception("could not write %s", self.path)

    def timeout(self) -> None:
        """The hard deadline: report (if nothing was yet) and leave, whatever is stuck."""
        self.fail(f"timeout: no result within {SMOKE_TIMEOUT:.0f} s")
        with contextlib.suppress(Exception):
            logging.shutdown()
        os._exit(0 if self.passed else 1)


def check_http(base: str, timeout: float = 15.0) -> str:
    """Check the API and the page the window shows. Returns details for the report."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            health = fetch_json(base + "/api/health")
            break
        except (OSError, ValueError) as exc:
            if time.monotonic() >= deadline:
                raise SmokeFailure(f"api/health: {exc}") from exc
            time.sleep(0.25)
    if health.get("status") != "ok":
        raise SmokeFailure(f"api/health: {health!r}")
    try:
        state = fetch_json(base + "/api/state")
    except (OSError, ValueError) as exc:
        raise SmokeFailure(f"api/state: {exc}") from exc
    if (state.get("me") or {}).get("kind") != "owner":
        raise SmokeFailure("api/state: this computer is not treated as the device's owner")
    try:
        status, body = fetch(base + "/")
    except OSError as exc:
        raise SmokeFailure(f"page: {exc}") from exc
    page = body.decode("utf-8", "replace")
    if status != 200 or "brand-name" not in page:
        raise SmokeFailure(f"page: HTTP {status}, no .brand-name in the HTML")
    assets = sorted(
        {html.unescape(m) for m in re.findall(r'(?:src|href)="(/static/[^"#]+)"', page)}
    )
    for asset in assets:
        try:
            asset_status, _ = fetch(base + asset)
        except OSError as exc:
            raise SmokeFailure(f"asset {asset}: {exc}") from exc
        if asset_status != 200:
            raise SmokeFailure(f"asset {asset}: HTTP {asset_status}")
    host = state.get("host") or {}
    return (
        f"version={health.get('version')} device={json.dumps(host.get('name'))} "
        f"discovery={'on' if state.get('discovery') else 'off'} assets={len(assets)}"
    )


def _smoke_main(args: argparse.Namespace) -> int:
    report = SmokeReport(args.smoke_report)
    deadline = threading.Timer(SMOKE_TIMEOUT, report.timeout)
    deadline.daemon = True
    deadline.start()
    scratch = Path(tempfile.mkdtemp(prefix="open-transfer-smoke-"))
    log_file = args.smoke_report.with_suffix(".log") if args.smoke_report else None
    try:
        with logging_to(log_file, args.verbose):
            try:
                _smoke(args, scratch, report)
            except Exception as exc:
                log.exception("smoke test crashed")
                report.fail(f"crash: {type(exc).__name__}: {exc}")
    finally:
        deadline.cancel()
        shutil.rmtree(scratch, ignore_errors=True)
    if not report.done:
        report.fail("no result")
    return 0 if report.passed else 1


def _smoke(args: argparse.Namespace, scratch: Path, report: SmokeReport) -> None:
    try:
        config = smoke_config(args, scratch)
    except ValueError as exc:
        report.fail(f"config: {exc}")
        return
    lock = InstanceLock(config.state_path)
    if not lock.acquire():
        report.fail(f"another copy is using {config.state_path}")
        return
    try:
        try:
            node = _start_node(config)
        except _StartError as exc:
            report.fail(f"start: {exc}")
            return
        lock.write_info(node.port)
        try:
            if args.no_window:
                report.ok(f"mode=no-window {check_http(node.local_url)}")
                return
            webview, why = load_webview()
            if webview is None:
                report.fail(f"{WINDOW_UNAVAILABLE}: pywebview is not available ({why})")
                return
            app = WindowApp(webview, node.local_url, private=True, load_timeout=SMOKE_LOAD_TIMEOUT)
            app.on_ready = lambda window_app: _smoke_window(window_app, report)
            app.run()
            if not report.done:
                reason = app.error or "the window closed before the check finished"
                report.fail(f"{WINDOW_UNAVAILABLE}: {reason}")
        finally:
            _Stopper(node).stop(wait=5.0)
    finally:
        lock.release()


def _smoke_window(app: WindowApp, report: SmokeReport) -> None:
    """Runs on a background thread while the window is up."""
    try:
        details = check_http(app.url)
        if not app.loaded.wait(app.load_timeout):
            raise SmokeFailure(
                f"{WINDOW_UNAVAILABLE}: {app.error or 'the page did not load in the native window'}"
            )
        try:
            value, how = app.evaluate(UI_PROBE, timeout=15.0)
        except Exception as exc:
            raise SmokeFailure(
                f"{WINDOW_UNAVAILABLE}: JavaScript failed in the native window: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if value is None:
            raise SmokeFailure(f"{WINDOW_UNAVAILABLE}: {how} returned nothing on this web view")
        if value != "ok":
            raise SmokeFailure(f"ui={value!r}: the native window shows a page without .brand-name")
        renderer = app.renderer or "unknown"
        report.ok(f"mode=window renderer={renderer} ui=ok probe={how} {details}")
    except SmokeFailure as exc:
        report.fail(str(exc))
    except Exception as exc:
        report.fail(f"{type(exc).__name__}: {exc}")
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
