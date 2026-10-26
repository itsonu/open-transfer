"""Keeping sync running with no window: the --background copy, handing over
to it when the window closes, a window onto it, and starting at login."""

from __future__ import annotations

import json
import os
import plistlib
import secrets
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from open_transfer import background, desktop, instance
from open_transfer.config import Config
from tests.test_desktop import FakeWebview, use_webview
from tests.test_mesh import Client, wait_for


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No real browser, and no OPEN_TRANSFER_* settings from the environment."""
    urls: list[str] = []
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url, *a, **k: urls.append(url))
    for name in list(os.environ):
        if name.startswith("OPEN_TRANSFER_"):
            monkeypatch.delenv(name)
    return urls


# ---------------------------------------------------------------- login items


def test_app_command() -> None:
    assert background.app_command("--background") == [
        sys.executable,
        "-m",
        "open_transfer.desktop",
        "--background",
    ]


def test_macos_login_item(tmp_path: Path) -> None:
    item = background.LoginItem(
        ["/Applications/Open Transfer.app/Contents/MacOS/Open Transfer", "--background"],
        platform="darwin",
        home=tmp_path,
    )
    assert item.supported
    assert not item.enabled()
    item.set(True)
    assert item.enabled()
    plist = plistlib.loads(
        (tmp_path / "Library/LaunchAgents" / f"{background.LABEL}.plist").read_bytes()
    )
    assert plist["ProgramArguments"][-1] == "--background"
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}  # restarted after a crash, not a Quit
    item.set(False)
    assert not item.enabled()
    item.set(False)  # twice is fine


def test_linux_login_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    item = background.LoginItem(
        ["/opt/open transfer/bin", "--background"], platform="linux", home=tmp_path
    )
    item.set(True)
    entry = (tmp_path / "cfg/autostart/open-transfer.desktop").read_text()
    assert "Exec='/opt/open transfer/bin' --background" in entry
    assert item.enabled()
    item.set(False)
    assert not item.enabled()


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows registry")
def test_windows_login_item() -> None:
    item = background.LoginItem(
        [r"C:\Program Files\Open Transfer\Open Transfer.exe", "--background"],
        name=f"Open Transfer test {secrets.token_hex(4)}",
    )
    try:
        item.set(True)
        assert item.enabled()
        assert (
            item._read_run() == r'"C:\Program Files\Open Transfer\Open Transfer.exe" --background'
        )
    finally:
        item.set(False)
    assert not item.enabled()


def test_unsupported_systems_say_so(tmp_path: Path) -> None:
    item = background.LoginItem(["x"], platform="sunos5", home=tmp_path)
    assert not item.supported
    with pytest.raises(OSError, match="isn’t supported"):
        item.set(True)


# ------------------------------------------------------- the running copy


def test_instance_info_says_whether_it_is_the_background_copy(tmp_path: Path) -> None:
    lock = instance.InstanceLock(tmp_path)
    assert lock.acquire()
    try:
        lock.write_info(4321, background=True)
        info = lock.read_info()
        assert info == instance.RunningInstance(
            pid=info.pid if info else 0, port=4321, background=True
        )
    finally:
        lock.release()


def test_opening_the_app_shows_a_window_onto_the_background_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    first = instance.InstanceLock(tmp_path)
    assert first.acquire()
    first.write_info(4567, background=True)
    monkeypatch.setattr(desktop, "is_alive", lambda inst: True)
    asked: list[float] = []
    monkeypatch.setattr(
        instance.InstanceLock, "request_focus", lambda self, timeout: asked.append(timeout) or False
    )
    fake = FakeWebview()
    use_webview(monkeypatch, fake)

    def user() -> None:
        assert fake.started.wait(10)
        fake.user_closes_window()

    closer = threading.Thread(target=user)
    closer.start()
    try:
        assert desktop.hand_off(instance.InstanceLock(tmp_path), wait=2) == 0
    finally:
        closer.join(5)
        first.release()
    assert asked == []  # no window there to bring forward: no 4-second wait
    assert fake.window is not None
    assert fake.window.url == "http://127.0.0.1:4567"
    assert opened == []


def test_a_second_background_copy_just_exits(tmp_path: Path, opened: list[str]) -> None:
    folder = tmp_path / "share"
    first = instance.InstanceLock(folder / ".open-transfer")
    assert first.acquire()
    try:
        assert desktop.main([str(folder), "--background"]) == 0
    finally:
        first.release()
    assert opened == []


def call(base: str, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    return Client(base)(method, path, body)


def test_the_background_copy_runs_until_quit(tmp_path: Path, opened: list[str]) -> None:
    folder = tmp_path / "share"
    state = folder / ".open-transfer"
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(
            desktop.main(
                [
                    str(folder),
                    "--background",
                    "--port",
                    "0",
                    "--host",
                    "127.0.0.1",
                    "--no-discovery",
                ]
            )
        )
    )
    thread.start()
    try:
        info = wait_for(lambda: instance.InstanceLock(state).read_info())
        assert info.background is True
        status, app = call(info.url, "GET", "/api/app")
        assert status == 200
        assert app["desktop"] is True
        assert app["background"] is True
        status, _ = call(info.url, "POST", "/api/app/quit", {})
        assert status == 200
        thread.join(15)
        assert result == [0]
    finally:
        thread.join(1)
    assert opened == []  # no window, no browser
    lock = instance.InstanceLock(state)
    assert lock.acquire()  # released
    lock.release()


def test_closing_the_window_hands_over_when_sync_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    fake = FakeWebview()
    use_webview(monkeypatch, fake)
    spawned: list[tuple[str, ...]] = []
    monkeypatch.setattr(background, "spawn", lambda *args: spawned.append(args) or True)
    folder = tmp_path / "share"
    state = folder / ".open-transfer"
    state.mkdir(parents=True)
    (state / "sync.json").write_text(json.dumps({"policy": {"enabled": True}}))

    def user() -> None:
        assert fake.started.wait(10)
        fake.user_closes_window()

    thread = threading.Thread(target=user)
    thread.start()
    args = [str(folder), "--port", "0", "--host", "127.0.0.1", "--no-discovery"]
    assert desktop.main(args) == 0
    thread.join(10)
    assert spawned == [("--background", *args)]


def test_closing_the_window_quits_when_sync_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    fake = FakeWebview()
    use_webview(monkeypatch, fake)
    spawned: list[tuple[str, ...]] = []
    monkeypatch.setattr(background, "spawn", lambda *args: spawned.append(args) or True)

    def user() -> None:
        assert fake.started.wait(10)
        fake.user_closes_window()

    thread = threading.Thread(target=user)
    thread.start()
    assert (
        desktop.main(
            [str(tmp_path / "share"), "--port", "0", "--host", "127.0.0.1", "--no-discovery"]
        )
        == 0
    )
    thread.join(10)
    assert spawned == []


# ------------------------------------------------------------------ /api/app


def test_the_app_api_outside_the_desktop_app(tmp_path: Path) -> None:
    from open_transfer.app import create_app

    app = create_app(Config(storage_dir=tmp_path, discovery=False, reserve_disk_bytes=0))
    client = app.test_client()
    assert client.get("/api/app").get_json() == {"desktop": False}
    assert client.put("/api/app", json={"start_at_login": True}).status_code == 404
    assert client.post("/api/app/quit", json={}).status_code == 404


def test_start_at_login_from_the_page(tmp_path: Path) -> None:
    from open_transfer.app import create_app

    app = create_app(Config(storage_dir=tmp_path, discovery=False, reserve_disk_bytes=0))
    login = background.LoginItem(["ot", "--background"], platform="darwin", home=tmp_path)
    app.extensions["open_transfer"]["desktop"] = background.Controls(background=False, login=login)
    client = app.test_client()
    assert client.get("/api/app").get_json() == {
        "desktop": True,
        "background": False,
        "can_start_at_login": True,
        "start_at_login": False,
    }
    assert client.put("/api/app", json={"start_at_login": "yes"}).status_code == 400
    assert (
        client.put("/api/app", json={"start_at_login": True}).get_json()["start_at_login"] is True
    )
    assert login.enabled()
    assert client.post("/api/app/quit", json={}).status_code == 404  # the window quits by closing
