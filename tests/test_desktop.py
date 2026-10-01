"""The desktop launcher's own logic: arguments, single instance, window lifecycle, smoke test.

Runs headless everywhere: pywebview is replaced by a small fake (``FakeWebview``)
that behaves like its event-driven API, so no display or web view is needed.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from open_transfer import desktop

SRC = Path(__file__).resolve().parents[1] / "src"


# ----------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """No real browser, no OPEN_TRANSFER_* settings from the environment, logging restored."""
    opened: list[str] = []
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url, *a, **k: opened.append(url))
    for name in list(os.environ):
        if name.startswith("OPEN_TRANSFER_"):
            monkeypatch.delenv(name)
    loggers = [logging.getLogger(n) for n in ("open_transfer", "pywebview")]
    saved = [(lg, lg.handlers[:], lg.level, lg.propagate) for lg in loggers]
    yield opened
    for lg, handlers, level, propagate in saved:
        lg.handlers[:] = handlers
        lg.setLevel(level)
        lg.propagate = propagate


@pytest.fixture
def opened(_isolate: list[str]) -> list[str]:
    return _isolate


class FakeEvent:
    """Like ``webview.event.Event``: handlers get the window or the event's arguments."""

    def __init__(self, window: FakeWindow) -> None:
        self.window = window
        self.handlers: list[Callable[..., Any]] = []
        self.flag = threading.Event()

    def __add__(self, handler: Callable[..., Any]) -> FakeEvent:
        self.handlers.append(handler)
        return self

    def set(self, *args: Any) -> bool:
        results = []
        for handler in self.handlers:
            params = inspect.signature(handler).parameters
            if not params:
                results.append(handler())
            elif "window" in params:
                results.append(handler(self.window, *args))
            else:
                results.append(handler(*args))
        self.flag.set()
        return any(result is False for result in results)

    def wait(self, timeout: float | None = None) -> bool:
        return self.flag.wait(timeout)

    def is_set(self) -> bool:
        return self.flag.is_set()


class FakeEvents:
    def __init__(self, window: FakeWindow) -> None:
        for name in ("initialized", "shown", "loaded", "closing", "closed"):
            setattr(self, name, FakeEvent(window))
        for name in ("minimized", "restored", "maximized"):
            setattr(self, name, FakeEvent(window))


class FakeWindow:
    def __init__(self, webview: FakeWebview, title: str, url: str, **options: Any) -> None:
        self.webview = webview
        self.title = title
        self.url = url
        self.options = options
        self.events = FakeEvents(self)
        self.gone = threading.Event()
        self.calls: list[str] = []

    def evaluate_js(self, script: str) -> Any:
        self.calls.append(f"evaluate_js:{script}")
        if self.webview.csp_blocks_eval:  # what WebView2/WKWebView do under our CSP
            raise RuntimeError(
                {
                    "name": "EvalError",
                    "message": "Refused to evaluate a string as JavaScript "
                    "because 'unsafe-eval' is not an allowed source of script",
                }
            )
        return self.webview.js_result

    def run_js(self, script: str) -> Any:
        self.calls.append(f"run_js:{script}")
        return self.webview.js_result

    def destroy(self) -> None:
        self.calls.append("destroy")
        self.gone.set()

    def show(self) -> None:
        self.calls.append("show")

    def restore(self) -> None:
        self.calls.append("restore")


class FakeWebview:
    """Just enough of the ``webview`` module for :class:`desktop.WindowApp`."""

    def __init__(self, renderer: str = "edgechromium", *, load: bool = True) -> None:
        self.settings = {"ALLOW_DOWNLOADS": False, "OPEN_EXTERNAL_LINKS_IN_BROWSER": True}
        self.renderer: str | None = None
        self._renderer = renderer
        self.load = load
        self.js_result: Any = "ok"
        self.csp_blocks_eval = False
        self.window: FakeWindow | None = None
        self.start_options: dict[str, Any] = {}
        self.started = threading.Event()

    def create_window(self, title: str, url: str, **options: Any) -> FakeWindow:
        self.window = FakeWindow(self, title, url, **options)
        return self.window

    def start(self, func: Callable[[], None] | None = None, **options: Any) -> None:
        assert self.window is not None
        window = self.window
        self.start_options = options
        self.renderer = self._renderer
        if window.events.initialized.set(self._renderer):
            return  # a handler cancelled the window, as pywebview does
        if func is not None:
            threading.Thread(target=func, daemon=True).start()
        window.events.shown.set()
        self.started.set()
        if self.load:
            window.events.loaded.set()
        assert window.gone.wait(30), "the window was never closed"
        window.events.closing.set()
        window.events.closed.set()

    def user_closes_window(self) -> None:
        assert self.started.wait(10)
        assert self.window is not None
        self.window.gone.set()


def use_webview(monkeypatch: pytest.MonkeyPatch, fake: FakeWebview | None) -> None:
    result = (fake, "") if fake else (None, "ModuleNotFoundError: No module named 'webview'")
    monkeypatch.setattr(desktop, "load_webview", lambda: result)


def smoke(tmp_path: Path, *extra: str) -> tuple[int, str]:
    report = tmp_path / "smoke.txt"
    code = desktop.main(["--smoke-test", "--no-discovery", "--smoke-report", str(report), *extra])
    return code, report.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------- arguments


def test_arguments_extend_the_cli(tmp_path: Path) -> None:
    args = desktop.parse_args([str(tmp_path), "--pin", "1234", "--no-window", "--smoke-test"])
    assert args.directory == str(tmp_path)
    assert args.pin == "1234"
    assert args.no_window
    assert args.smoke_test
    assert args.smoke_report is None
    plain = desktop.parse_args([])
    assert not plain.no_window
    assert not plain.smoke_test


def test_finder_process_serial_number_is_ignored() -> None:
    assert desktop.clean_argv(["-psn_0_12345", "--no-window"]) == ["--no-window"]
    assert not desktop.parse_args(["-psn_0_987"]).smoke_test


def test_smoke_config_uses_a_scratch_device(tmp_path: Path) -> None:
    config = desktop.smoke_config(desktop.parse_args(["--smoke-test"]), tmp_path)
    assert config.storage_dir == (tmp_path / "files").resolve()
    assert config.state_path == (tmp_path / "state").resolve()
    assert config.port == 0
    assert config.host == "127.0.0.1"

    folder = tmp_path / "mine"
    given = desktop.parse_args(["--smoke-test", str(folder), "--port", "5123", "--host", "0.0.0.0"])
    config = desktop.smoke_config(given, tmp_path)
    assert config.storage_dir == folder.resolve()
    assert config.state_path == (tmp_path / "state").resolve()  # never the real identity
    assert (config.port, config.host) == (5123, "0.0.0.0")


# ----------------------------------------------------------- single instance


def test_only_one_instance_per_state_folder(tmp_path: Path) -> None:
    first, second = desktop.InstanceLock(tmp_path), desktop.InstanceLock(tmp_path)
    assert first.acquire()
    assert first.held
    assert not first.advisory
    assert not second.acquire()
    first.write_info(4321)
    info = second.read_info()
    assert info == desktop.RunningInstance(pid=os.getpid(), port=4321)
    assert info.url == "http://127.0.0.1:4321"
    first.release()
    assert not first.info_path.exists()
    assert second.acquire()
    second.release()


def test_lock_held_by_another_process(tmp_path: Path) -> None:
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import sys, time
                sys.path.insert(0, {str(SRC)!r})
                from open_transfer.desktop import InstanceLock
                lock = InstanceLock(sys.argv[1])
                assert lock.acquire()
                lock.write_info(1234)
                print("locked", flush=True)
                sys.stdin.readline()
                """
            ),
            str(tmp_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        lock = desktop.InstanceLock(tmp_path)
        assert not lock.acquire()
        info = lock.read_info()
        assert info is not None
        assert info.port == 1234
        assert info.pid == holder.pid
    finally:
        holder.communicate("\n", timeout=10)
    # The OS dropped the lock when the process exited.
    lock = desktop.InstanceLock(tmp_path)
    assert lock.acquire()
    lock.release()


def test_lock_falls_back_to_instance_file_without_os_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desktop, "_try_lock", lambda fh: None)
    (tmp_path / desktop.INFO_FILE).write_text(json.dumps({"pid": 999999, "port": 4000}))
    monkeypatch.setattr(desktop, "is_alive", lambda instance: True)
    assert not desktop.InstanceLock(tmp_path).acquire()  # someone answers on that port

    monkeypatch.setattr(desktop, "is_alive", lambda instance: False)
    lock = desktop.InstanceLock(tmp_path)
    assert lock.acquire()  # stale file from a crashed run
    assert lock.advisory
    lock.release()


@pytest.mark.parametrize("content", ["", "{", '{"pid": 1}', '{"pid": 1, "port": 0}', "[1, 2]"])
def test_bad_instance_file_is_ignored(tmp_path: Path, content: str) -> None:
    (tmp_path / desktop.INFO_FILE).write_text(content)
    assert desktop.InstanceLock(tmp_path).read_info() is None


def test_focus_requests_are_answered(tmp_path: Path) -> None:
    lock = desktop.InstanceLock(tmp_path)
    assert lock.acquire()
    shown: list[bool] = []
    watcher = desktop.FocusRequests(lock.focus_path, lambda: shown.append(True), interval=0.05)
    watcher.start()
    try:
        assert desktop.InstanceLock(tmp_path).request_focus(timeout=5)
    finally:
        watcher.stop()
    assert shown == [True]
    assert not lock.focus_path.exists()
    lock.release()


def test_failed_focus_request_is_not_confirmed(tmp_path: Path) -> None:
    def broken() -> None:
        raise RuntimeError("no window")

    path = tmp_path / desktop.FOCUS_FILE
    path.write_text("x")
    assert not desktop.FocusRequests(path, broken).poll()
    assert path.exists()  # the second copy times out and opens the browser
    assert not desktop.FocusRequests(tmp_path / "none", broken).poll()


def test_second_copy_brings_the_first_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    first = desktop.InstanceLock(tmp_path)
    assert first.acquire()
    first.write_info(4567)
    monkeypatch.setattr(desktop, "is_alive", lambda instance: instance.port == 4567)
    shown: list[bool] = []
    watcher = desktop.FocusRequests(first.focus_path, lambda: shown.append(True), interval=0.05)
    watcher.start()
    try:
        second = desktop.InstanceLock(tmp_path)
        assert not second.acquire()
        assert desktop.hand_off(second, wait=2) == 0
    finally:
        watcher.stop()
    assert shown == [True]
    assert opened == []

    # A copy that doesn't answer (browser mode with no watcher, say): open its page instead.
    assert desktop.hand_off(desktop.InstanceLock(tmp_path), wait=2, ack_timeout=0.3) == 0
    assert opened == ["http://127.0.0.1:4567"]
    first.release()


def test_second_copy_gives_up_on_an_unresponsive_first(tmp_path: Path, opened: list[str]) -> None:
    lock = desktop.InstanceLock(tmp_path)
    started = time.monotonic()
    assert desktop.hand_off(lock, wait=0.5) == 1
    assert time.monotonic() - started < 5
    assert opened == []


def test_main_hands_off_to_a_running_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    folder = tmp_path / "share"
    first = desktop.InstanceLock(folder / ".open-transfer")
    assert first.acquire()
    first.write_info(4999)
    monkeypatch.setattr(desktop, "is_alive", lambda instance: True)
    monkeypatch.setattr(desktop.InstanceLock, "request_focus", lambda self, timeout: False)
    assert desktop.main([str(folder)]) == 0
    assert opened == ["http://127.0.0.1:4999"]
    first.release()


# ---------------------------------------------------------------- smoke test


def test_smoke_test_in_a_native_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeWebview()
    use_webview(monkeypatch, fake)
    code, line = smoke(tmp_path)
    assert code == 0, line
    assert line.startswith("SMOKE OK mode=window renderer=edgechromium ui=ok probe=evaluate_js ")
    assert "discovery=off" in line
    assert fake.window is not None
    assert f"evaluate_js:{desktop.UI_PROBE}" in fake.window.calls
    assert fake.window.url.startswith("http://127.0.0.1:")
    assert fake.window.options["min_size"] == desktop.MIN_SIZE
    assert fake.window.options["text_select"] is True
    assert fake.settings["ALLOW_DOWNLOADS"] is True
    assert fake.start_options["private_mode"] is True
    if sys.platform == "win32":
        assert fake.start_options["gui"] == "edgechromium"
    assert (tmp_path / "smoke.log").exists()


@pytest.mark.parametrize("result", ["ok", "missing"])
def test_smoke_probe_works_under_the_pages_csp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: str
) -> None:
    """The UI's CSP forbids eval(), which evaluate_js uses: the probe falls back to run_js."""
    fake = FakeWebview()
    fake.csp_blocks_eval = True
    fake.js_result = result
    use_webview(monkeypatch, fake)
    code, line = smoke(tmp_path)
    assert fake.window is not None
    assert f"run_js:{desktop.UI_PROBE}" in fake.window.calls
    if result == "ok":
        assert code == 0, line
        assert " ui=ok probe=run_js " in line
    else:
        assert code == 1
        assert line.startswith("SMOKE FAIL ui='missing'")


def test_smoke_test_fails_when_the_ui_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeWebview()
    fake.js_result = "missing"
    use_webview(monkeypatch, fake)
    code, line = smoke(tmp_path)
    assert code == 1
    assert line.startswith("SMOKE FAIL ui='missing'")


@pytest.mark.parametrize(
    ("fake", "reason"),
    [
        (None, "pywebview is not available"),
        (FakeWebview("mshtml"), "WebView2 Runtime is not installed"),
    ],
)
def test_smoke_test_without_a_native_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeWebview | None, reason: str
) -> None:
    use_webview(monkeypatch, fake)
    code, line = smoke(tmp_path)
    assert code == 1
    assert line.startswith(f"SMOKE FAIL {desktop.WINDOW_UNAVAILABLE}:")
    assert reason in line


def test_smoke_test_when_the_page_never_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desktop, "SMOKE_LOAD_TIMEOUT", 0.5)
    use_webview(monkeypatch, FakeWebview(load=False))
    code, line = smoke(tmp_path)
    assert code == 1
    assert line.startswith(f"SMOKE FAIL {desktop.WINDOW_UNAVAILABLE}:")


def test_smoke_test_in_browser_mode(tmp_path: Path, opened: list[str]) -> None:
    code, line = smoke(tmp_path, "--no-window")
    assert code == 0, line
    assert line.startswith("SMOKE OK mode=no-window version=")
    assert "assets=" in line
    assert opened == []  # a smoke test never opens a browser


def test_smoke_report_keeps_the_first_result(tmp_path: Path) -> None:
    report = desktop.SmokeReport(tmp_path / "r.txt")
    report.fail("one\ntwo")
    report.ok("later")
    assert not report.passed
    assert (tmp_path / "r.txt").read_text() == "SMOKE FAIL one two\n"


# -------------------------------------------------------------------- the app


def test_closing_the_window_stops_sharing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    fake = FakeWebview()
    use_webview(monkeypatch, fake)
    folder = tmp_path / "share"
    state = folder / ".open-transfer"
    seen: dict[str, Any] = {}

    def user() -> None:
        assert fake.started.wait(10)
        info = desktop.InstanceLock(state).read_info()
        assert info is not None
        seen["health"] = desktop.is_alive(info)
        seen["second"] = desktop.InstanceLock(state).acquire()
        # A second copy asks the first to come forward: the window is shown.
        seen["focus"] = desktop.InstanceLock(state).request_focus(timeout=5)
        fake.user_closes_window()

    thread = threading.Thread(target=user)
    thread.start()
    code = desktop.main([str(folder), "--port", "0", "--host", "127.0.0.1", "--no-discovery"])
    thread.join(10)
    assert code == 0
    assert seen == {"health": True, "second": False, "focus": True}
    assert fake.window is not None
    assert "show" in fake.window.calls
    assert opened == []
    assert fake.start_options["private_mode"] is False
    assert not (state / desktop.INFO_FILE).exists()
    assert (state / desktop.LOG_FILE).read_text(encoding="utf-8").strip()
    lock = desktop.InstanceLock(state)
    assert lock.acquire()  # released
    lock.release()


@pytest.mark.parametrize("fake", [None, FakeWebview("mshtml")])
def test_falls_back_to_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str], fake: FakeWebview | None
) -> None:
    use_webview(monkeypatch, fake)
    waited: list[str] = []
    monkeypatch.setattr(
        desktop, "wait_until_quit", lambda url, folder, open_url: waited.append(url)
    )
    code = desktop.main(
        [str(tmp_path / "share"), "--port", "0", "--host", "127.0.0.1", "--no-discovery"]
    )
    assert code == 0
    assert len(waited) == 1
    assert waited[0].startswith("http://127.0.0.1:")
    assert opened == waited


def test_no_window_and_no_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    fake = FakeWebview()
    use_webview(monkeypatch, fake)
    waited: list[str] = []
    monkeypatch.setattr(
        desktop, "wait_until_quit", lambda url, folder, open_url: waited.append(url)
    )
    args = [str(tmp_path), "--no-window", "--no-browser", "--port", "0", "--no-discovery"]
    assert desktop.main(args) == 0
    assert fake.window is None
    assert len(waited) == 1
    assert opened == []


def test_bad_options_exit_with_an_error(tmp_path: Path) -> None:
    assert desktop.main([str(tmp_path), "--pin", "1"]) == 2
