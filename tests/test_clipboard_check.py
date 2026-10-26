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
