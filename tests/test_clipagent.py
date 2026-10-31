"""Native clipboard agents and the watcher that feeds the sync engine.

The OS agents themselves are exercised on the CI runner of their own platform
(macOS, Windows; Linux when a clipboard tool and display are there); the rest
uses a stand-in clipboard. The last tests run two apps with no window at all —
desktop-to-desktop sync with both windows closed.
"""

from __future__ import annotations

import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from open_transfer import clipagent
from open_transfer.clipagent import CommandAgent, Snapshot, Watcher
from open_transfer.node import Node
from tests.conftest import NodeFactory
from tests.test_mesh import owner, wait_for


class FakeAgent:
    """A clipboard in memory, with the markers a real one carries."""

    name = "fake"
    reads_to_detect = False

    def __init__(self) -> None:
        self.text: str | None = None
        self.count = 0
        self.concealed = False
        self.ours = False
        self.source = ""
        self.reads = 0
        self.broken = 0  # fail this many times, like a busy clipboard

    def user_copies(self, text: str, *, concealed: bool = False, source: str = "") -> None:
        self.text, self.concealed, self.ours, self.source = text, concealed, False, source
        self.count += 1

    def change_token(self) -> object:
        if self.broken:
            self.broken -= 1
            raise OSError("the clipboard is busy")
        return self.count

    def read(self) -> Snapshot:
        self.reads += 1
        return Snapshot(self.text, concealed=self.concealed, ours=self.ours, source=self.source)

    def write(self, text: str, *, sensitive: bool) -> bool:
        self.text, self.ours, self.concealed = text, True, sensitive
        self.count += 1
        return True


class Engine:
    """Records what the watcher reports."""

    def __init__(self, active: bool = True) -> None:
        self.copies: list[dict[str, Any]] = []
        self.on = active
        self.writer: Any = None
        self.before_apply: Any = None

    def active(self) -> bool:
        return self.on

    def copy(self, text: str, **kw: Any) -> dict[str, Any]:
        self.copies.append({"text": text, **kw})
        return {"status": "sent"}

    def set_writer(self, writer: Any) -> None:
        self.writer = writer


def watcher(agent: Any, engine: Any) -> Watcher:
    w = Watcher(agent, engine, interval=0.01)
    w.start()
    return w


def test_a_new_copy_is_reported_once() -> None:
    agent, engine = FakeAgent(), Engine()
    agent.user_copies("already there")  # before the agent started: not a new copy
    w = watcher(agent, engine)
    try:
        agent.user_copies("hello", source="com.apple.TextEdit")
        wait_for(lambda: engine.copies, timeout=2)
        time.sleep(0.05)
        assert engine.copies == [
            {"text": "hello", "source": "com.apple.TextEdit", "concealed": False}
        ]
    finally:
        w.stop()


def test_clips_it_writes_are_not_reported_back() -> None:
    agent, engine = FakeAgent(), Engine()
    w = watcher(agent, engine)
    try:
        assert engine.writer("from the Mac", {"sensitive": True}) is True
        assert agent.text == "from the Mac"
        assert agent.concealed is True
        time.sleep(0.1)
        assert engine.copies == []
    finally:
        w.stop()


def test_password_manager_copies_are_flagged() -> None:
    agent, engine = FakeAgent(), Engine()
    w = watcher(agent, engine)
    try:
        agent.user_copies("hunter2", concealed=True)
        wait_for(lambda: engine.copies, timeout=2)
        assert engine.copies[0]["concealed"] is True
    finally:
        w.stop()


def test_nothing_is_read_while_sync_is_off() -> None:
    agent, engine = FakeAgent(), Engine(active=False)
    w = watcher(agent, engine)
    try:
        agent.user_copies("private")
        time.sleep(0.1)
        assert agent.reads == 0
        assert engine.copies == []
    finally:
        w.stop()


def test_it_keeps_going_when_the_clipboard_is_busy() -> None:
    agent, engine = FakeAgent(), Engine()
    w = watcher(agent, engine)
    try:
        agent.broken = 3
        agent.user_copies("after a hiccup")
        wait_for(lambda: engine.copies, timeout=3)
        assert w.failures == 0
    finally:
        w.stop()


def test_before_apply_reports_a_pending_copy() -> None:
    agent, engine = FakeAgent(), Engine()
    w = Watcher(agent, engine, interval=60)  # the loop won't get to it first
    w.start()
    try:
        agent.user_copies("just copied")
        engine.before_apply()
        assert engine.copies[0]["text"] == "just copied"
    finally:
        w.stop()


# ------------------------------------------------- command-line clipboards


FAKE_TOOL = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    store = Path(sys.argv[2])
    if sys.argv[1] == "paste":
        if not store.exists():
            sys.exit(1)
        sys.stdout.buffer.write(store.read_bytes())
    elif sys.argv[1] == "types":
        sys.stdout.write("text/plain\\n" + ("x-kde-passwordManagerHint\\n" if Path(str(store) + ".secret").exists() else ""))
    else:
        store.write_bytes(sys.stdin.buffer.read())
    """
)


def test_command_line_clipboards(tmp_path: Path) -> None:
    tool = tmp_path / "tool.py"
    tool.write_text(FAKE_TOOL)
    store = tmp_path / "clip"
    run = [sys.executable, str(tool)]
    agent = CommandAgent(
        "fake-tool",
        [*run, "paste", str(store)],
        [*run, "copy", str(store)],
        types=[*run, "types", str(store)],
        private_type="x-kde-passwordManagerHint",
    )
    assert agent.change_token() is None  # empty clipboard
    wrote = agent.write("héllo 👋", sensitive=False)
    assert wrote is True
    first = agent.change_token()
    assert agent.read() == Snapshot("héllo 👋")
    store.write_text("other")
    assert agent.change_token() != first
    Path(str(store) + ".secret").touch()
    assert agent.read().concealed is True


def test_detect_picks_nothing_without_a_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("WAYLAND_DISPLAY", "DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    assert clipagent.detect("linux") is None
    assert clipagent.detect("sunos5") is None


# ---------------------------------------------------- the real thing (CI)


def real_agent() -> clipagent.Agent:
    agent = clipagent.detect()
    if agent is None:
        pytest.skip("no clipboard on this machine")
    try:
        agent.change_token()
    except Exception as exc:  # a CI session without a clipboard
        pytest.skip(f"clipboard not usable here: {exc}")
    return agent


def test_the_native_clipboard_round_trip() -> None:
    agent = real_agent()
    before = agent.change_token()
    text = f"Open Transfer test {time.time()} ✓"
    wrote = False
    try:
        wrote = agent.write(text, sensitive=False)
    except OSError as exc:
        pytest.skip(f"clipboard not writable here: {exc}")
    assert wrote is True
    snap = agent.read()
    assert snap.text == text
    if agent.name in ("macos", "windows"):
        assert snap.ours is True  # tagged, so the watcher won't send it back
        assert agent.change_token() != before


def test_private_writes_are_kept_out_of_clipboard_history() -> None:
    agent = real_agent()
    if agent.name not in ("macos", "windows"):
        pytest.skip("markers are a macOS/Windows feature")
    try:
        agent.write("a secret", sensitive=True)
    except OSError as exc:
        pytest.skip(f"clipboard not writable here: {exc}")
    assert agent.read().concealed is True


# ------------------------------------------- two apps, no window anywhere


def sync_on(node: Node) -> None:
    status, data = owner(node)("PUT", "/api/sync/policy", {"enabled": True})
    assert status == 200, data


def test_desktops_sync_both_ways_with_no_window(make_node: NodeFactory) -> None:
    mac, win = make_node("Mac"), make_node("Windows")
    for node in (mac, win):
        sync_on(node)
    code = owner(win)("POST", "/api/pair/open")[1]["code"]
    status, data = owner(mac)(
        "POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{win.port}"}
    )
    assert status == 200, data
    mac_clip, win_clip = FakeAgent(), FakeAgent()
    for node, agent in ((mac, mac_clip), (win, win_clip)):
        node.start_clipboard(agent).interval = 0.02
    try:
        mac_clip.user_copies("copied on the Mac")
        wait_for(lambda: win_clip.text == "copied on the Mac")
        assert win_clip.ours  # written by the agent: not echoed back
        win_clip.user_copies("copied on Windows")
        wait_for(lambda: mac_clip.text == "copied on Windows")
        time.sleep(0.2)
        assert win_clip.text == "copied on Windows"  # nothing bounced back
        status, data = owner(mac)("GET", "/api/sync")
        assert data["clipboard_access"] is True
        assert [r["direction"] for r in data["recent"][:2]] == ["received", "sent"]
    finally:
        for node in (mac, win):
            assert node.watcher is not None
            node.watcher.stop()


def test_a_clip_lands_on_the_real_clipboard_with_no_window(make_node: NodeFactory) -> None:
    # K4's last hop: a copy from another device ends up on this computer's own clipboard
    # (on Windows CI, the real Windows clipboard), with no window open.
    agent = real_agent()
    mac, pc = make_node("Mac"), make_node("PC")
    for node in (mac, pc):
        sync_on(node)
    code = owner(pc)("POST", "/api/pair/open")[1]["code"]
    status, data = owner(mac)(
        "POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{pc.port}"}
    )
    assert status == 200, data
    mac_clip = FakeAgent()
    mac.start_clipboard(mac_clip).interval = 0.02
    pc.start_clipboard(agent).interval = 0.02
    text = f"to the PC {time.time()} ✓"
    try:
        mac_clip.user_copies(text)
        wait_for(lambda: agent.read().text == text)
        assert agent.read().ours is (agent.name in ("macos", "windows"))  # not sent back
    finally:
        for node in (mac, pc):
            assert node.watcher is not None
            node.watcher.stop()


def test_the_clipboard_flag_starts_the_native_agent(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeAgent()
    monkeypatch.setattr(clipagent, "detect", lambda platform=None: fake)
    node = make_node("Agent", clipboard=True)
    assert node.watcher is not None
    assert node.watcher.agent is fake
    assert node.mesh.sync.can_write
    node.stop()
    assert not node.mesh.sync.can_write
