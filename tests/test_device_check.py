"""scripts/device_check.py — the real-device checklist — run against three nodes here."""

from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path
from types import ModuleType

from open_transfer.config import Config
from open_transfer.node import Node

ROOT = Path(__file__).resolve().parents[1]


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("device_check", ROOT / "scripts/device_check.py")
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["device_check"] = module  # dataclasses look their module up there
    spec.loader.exec_module(module)
    return module


def test_device_check_passes_against_real_nodes(tmp_path: Path) -> None:
    script = load_script()
    discovery_port = random.randint(40000, 59000)
    nodes = []
    try:
        for name, form in (("Desk", "computer"), ("Phone", "phone"), ("Tab", "tablet")):
            node = Node(
                Config(
                    storage_dir=tmp_path / name,
                    port=0,
                    device_name=name,
                    device_form=form,
                    discovery_port=discovery_port,
                    reserve_disk_bytes=0,
                )
            )
            node.start()
            nodes.append(node)
        out = tmp_path / "report.md"
        code = script.main(
            [
                "--no-adb",
                "--pc-url",
                nodes[0].local_url,
                *("--node", nodes[1].local_url, "--node", nodes[2].local_url),
                *("--big-mb", "1", "--skip-slow", "--out", str(out)),
            ]
        )
        report = out.read_text(encoding="utf-8")
        assert code == 0, report
        assert "| ❌ |" not in report
        for ref in ("1.1", "1.3", "2.2", "2.3", "2.4", "2.5", "2.6", "4.2", "4.4", "4.7", "6.4"):
            assert f"| ✅ | {ref} |" in report, ref
        # Loopback can be fast enough to finish before the cancel; never a failure.
        assert "| ✅ | 6.3 |" in report or "| ⏭️ | 6.3 |" in report
        # Received test files are cleaned up.
        for node in nodes:
            assert not [p for p in node.config.storage_dir.iterdir() if p.name.startswith("ot-")]
    finally:
        for node in nodes:
            node.stop()


def test_report_lists_manual_checks() -> None:
    script = load_script()
    report = script.Report()
    report.add("1.1", "sees others", True)
    report.add("3", "A → B", False, "content differs")
    report.manual("4.3", "confirm dialog", "on screen")
    text = report.markdown()
    assert "**1 passed · 1 failed · 0 skipped · 1 need a person**" in text
    assert "| ❌ | 3 | A → B | content differs |" in text
    assert "|  | 4.3 | confirm dialog | on screen |" in text
