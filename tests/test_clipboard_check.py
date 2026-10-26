"""scripts/clipboard_check.py — the clipboard part of the real-device check —
against two apps here, with stand-in clipboards (Android needs adb, so those
checks run on real devices only)."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.conftest import NodeFactory
from tests.test_clipagent import FakeAgent
from tests.test_mesh import owner, wait_for

ROOT = Path(__file__).resolve().parents[1]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up there
    spec.loader.exec_module(module)
    return module


class AgentClipboard:
    """The script's view of this computer's clipboard, backed by the stand-in agent."""

    def __init__(self, agent: FakeAgent) -> None:
        self.agent = agent

    def read(self) -> str:
        return self.agent.text or ""

    def write(self, text: str) -> None:
        self.agent.user_copies(text)


def args(base: str, **extra: object) -> argparse.Namespace:
    values = {
        "pc_url": base,
        "adb": None,
        "apk": None,
        "serial": None,
        "no_input": True,
        "no_adb": True,
        "out": None,
    }
    return argparse.Namespace(**{**values, **extra})


def test_against_two_computers(make_node: NodeFactory, tmp_path: Path) -> None:
    load("device_check")
    script = load("clipboard_check")
    here = make_node("Mac")
    there = make_node("Windows", device_platform="windows")
    here_clip, there_clip = FakeAgent(), FakeAgent()
    here.start_clipboard(here_clip).interval = 0.02
    there.start_clipboard(there_clip).interval = 0.02
    owner(there)("PUT", "/api/sync/policy", {"enabled": True})
    code = owner(there).state()["pairing"]["code"]
    owner(here)("POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{there.port}"})
    wait_for(lambda: there.mesh.id in here.mesh._online_ids())

    report = script.Report()
    checker = script.Checker(
        args(here.local_url), report, tmp_path, clipboard=AgentClipboard(here_clip)
    )
    checker.setup()  # turns sync on here
    checker.run()
    statuses = {r.ref: r.status for r in report.results}
    assert statuses["C1"] == "✅"
    assert there_clip.text is not None
    assert there_clip.text.startswith("Open Transfer check ")
    assert {"C2", "C3", "D1"} <= set(statuses)  # left for a person (no input)
    assert not report.failed


def test_asking_for_help_with_another_computer(
    make_node: NodeFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    load("device_check")
    script = load("clipboard_check")
    here, there = make_node("Mac"), make_node("PC", device_platform="windows")
    here_clip, there_clip = FakeAgent(), FakeAgent()
    here.start_clipboard(here_clip).interval = 0.02
    there.start_clipboard(there_clip).interval = 0.02
    owner(there)("PUT", "/api/sync/policy", {"enabled": True})
    code = owner(there).state()["pairing"]["code"]
    owner(here)("POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{there.port}"})
    wait_for(lambda: there.mesh.id in here.mesh._online_ids())

    def person(question: str) -> str:
        return "y" if there_clip.text and there_clip.text in question else "n"

    real_print = print
    printed: list[str] = []

    def watch(*parts: object, **kw: object) -> None:
        text = " ".join(str(p) for p in parts)
        printed.append(text)
        if "Open Transfer check " in text:
            code = text[text.index("Open Transfer check ") :].strip()
            there_clip.user_copies(code)  # the person copies the code over there
        real_print(*parts, **kw)  # type: ignore[call-overload]

    report = script.Report()
    checker = script.Checker(
        args(here.local_url, no_input=False),
        report,
        tmp_path,
        clipboard=AgentClipboard(here_clip),
        ask=person,
    )
    checker.setup()
    monkeypatch.setattr(script, "print", watch, raising=False)
    checker.computers()
    statuses = {r.ref: r.status for r in report.results}
    assert statuses == {"C1": "✅", "C2": "✅", "C3": "✅"}


def test_an_app_without_clipboard_access_is_explained(
    make_node: NodeFactory, tmp_path: Path
) -> None:
    load("device_check")
    script = load("clipboard_check")
    node = make_node("Server")
    checker = script.Checker(
        args(node.local_url), script.Report(), tmp_path, clipboard=AgentClipboard(FakeAgent())
    )
    with pytest.raises(script.CheckError, match="desktop app"):
        checker.setup()


def test_markers_have_lengths_of_their_own() -> None:
    load("device_check")
    script = load("clipboard_check")
    used: set[int] = set()
    texts = [script.marker(used) for _ in range(20)]
    assert len({len(t) for t in texts}) == 20
    from open_transfer.sensitive import looks_sensitive

    assert all(looks_sensitive(t) is None for t in texts)


class FakeAdb:
    """adb for a stand-in phone: the share sheet sends through the phone's app."""

    def __init__(self, phone_owner) -> None:  # type: ignore[no-untyped-def]
        self.phone = phone_owner
        self.commands: list[str] = []

    def shell(self, serial: str, command: str, **_: object) -> str:
        self.commands.append(command)
        if "ClipboardSendActivity" in command and "extra.TEXT" in command:
            text = command.split("extra.TEXT '", 1)[1].split("'", 1)[0]
            self.phone("POST", "/api/sync/send", {"text": text})
        if command.startswith("pidof"):
            return "4242\n"
        return ""


def three_devices(make_node: NodeFactory):  # type: ignore[no-untyped-def]
    """A Mac (this computer), a Windows PC and an Android phone, all paired, syncing."""
    mac = make_node("Mac")
    pc = make_node("Windows", device_platform="windows")
    phone = make_node("Pixel", device_platform="android", device_form="phone")
    clips = {}
    for node in (mac, pc, phone):
        clips[node] = FakeAgent()
        node.start_clipboard(clips[node]).interval = 0.02
        owner(node)("PUT", "/api/sync/policy", {"enabled": True})
    for a, b in ((mac, pc), (mac, phone), (pc, phone)):
        code = owner(b).state()["pairing"]["code"]
        owner(a)("POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{b.port}"})
    for a, b in ((mac, pc), (mac, phone), (pc, phone), (phone, pc)):
        wait_for(lambda a=a, b=b: b.mesh.id in a.mesh._online_ids())
    return mac, pc, phone, clips


def checker_for(script, mac, phone, clips, tmp_path, ask):  # type: ignore[no-untyped-def]
    checker = script.Checker(
        args(mac.local_url, no_input=False),
        script.Report(),
        tmp_path,
        clipboard=AgentClipboard(clips[mac]),
        ask=ask,
    )
    checker.setup()
    checker.adb = FakeAdb(owner(phone))
    node = script.Node("Pixel", phone.local_url, "android", serial="emulator-5554")
    node.refresh()
    checker.androids = [node]
    return checker


def test_the_phone_and_the_other_computer_both_ways(
    make_node: NodeFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    load("device_check")
    script = load("clipboard_check")
    mac, pc, phone, clips = three_devices(make_node)

    def person(question: str) -> str:
        return "y" if clips[pc].text and clips[pc].text in question else "n"

    def watch(*parts: object, **_: object) -> None:
        text = " ".join(str(p) for p in parts)
        if "copy this text" in text and "Open Transfer check " in text:
            clips[pc].user_copies(text[text.index("Open Transfer check ") :].strip())

    checker = checker_for(script, mac, phone, clips, tmp_path, person)
    monkeypatch.setattr(script, "print", watch, raising=False)
    checker.crossings()
    statuses = {r.ref: r.status for r in checker.report.results}
    assert statuses == {"W1": "✅", "W2": "✅"}, checker.report.results
    assert clips[phone].text == clips[pc].text  # Windows → phone landed on its clipboard


def test_one_copy_reaches_every_device(make_node: NodeFactory, tmp_path: Path) -> None:
    load("device_check")
    script = load("clipboard_check")
    mac, pc, phone, clips = three_devices(make_node)
    checker = checker_for(script, mac, phone, clips, tmp_path, lambda q: "")
    checker.everyone()
    [result] = checker.report.results
    assert (result.ref, result.status) == ("M1", "✅"), result
    assert clips[pc].text == clips[phone].text == clips[mac].text


def test_one_copy_reports_who_missed_it(make_node: NodeFactory, tmp_path: Path) -> None:
    load("device_check")
    script = load("clipboard_check")
    mac, pc, phone, clips = three_devices(make_node)
    checker = checker_for(script, mac, phone, clips, tmp_path, lambda q: "")
    owner(mac)("PUT", f"/api/sync/devices/{pc.mesh.id}", {"excluded": True})
    checker.everyone()
    [result] = checker.report.results
    assert result.status == "❌"
    assert "Windows" in result.note
    assert "Pixel" not in result.note


def test_the_app_swiped_away_still_gets_clips(make_node: NodeFactory, tmp_path: Path) -> None:
    load("device_check")
    script = load("clipboard_check")
    mac, _pc, phone, clips = three_devices(make_node)
    asked: list[str] = []
    checker = checker_for(script, mac, phone, clips, tmp_path, lambda q: asked.append(q) or "")
    checker.swiped_away(checker.androids[0])
    statuses = {r.ref: r.status for r in checker.report.results}
    assert statuses == {"A7": "✅"}
    assert any("Recents" in q for q in asked)  # a person swipes it away


def test_without_a_person_those_are_left_to_do(make_node: NodeFactory, tmp_path: Path) -> None:
    load("device_check")
    script = load("clipboard_check")
    mac, _pc, phone, clips = three_devices(make_node)
    checker = checker_for(script, mac, phone, clips, tmp_path, lambda q: pytest.fail(q))
    checker.args.no_input = True
    checker.swiped_away(checker.androids[0])
    checker.crossings()
    by_ref = {r.ref: r.status for r in checker.report.results}
    assert by_ref["W1"] == "✅"  # its app accepting it needs nobody
    assert {"A7", "W2"} <= set(by_ref)
    assert not checker.report.failed
