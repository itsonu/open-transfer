"""The Python half of the Android app: what Kotlin calls, and what it's told.

Runs on any OS: ``open_transfer.android`` is plain Python; Kotlin only calls it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from open_transfer import android
from tests.conftest import NodeFactory
from tests.test_mesh import owner


class Listener:
    """Stands in for the Kotlin PythonEvents object."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def onEvent(self, name: str, data: str) -> None:  # the Kotlin method's name
        self.events.append((name, json.loads(data)))


@pytest.fixture
def phone(tmp_path: Path) -> Iterator[Listener]:
    listener = Listener()
    android.start(
        str(tmp_path / "Download"),
        str(tmp_path / "state"),
        "Pixel",
        "phone",
        listener,
        port=0,
        discovery=False,
    )
    yield listener
    android.stop()


def test_sync_state_without_reading_anything(phone: Listener) -> None:
    assert json.loads(android.sync_state()) == {"active": False, "mode": "auto", "clipboard": True}


def test_clips_that_arrive_go_to_kotlin(phone: Listener) -> None:
    assert android._node is not None
    sync = android._node.mesh.sync
    assert sync.can_write  # Android writes the clipboard from its service
    assert sync._writer is not None
    assert sync._writer("from the Mac", {"sensitive": True, "from": "Mac", "type": "text"}) is True
    assert phone.events[-1] == (
        "clipboard",
        {"text": "from the Mac", "sensitive": True, "from": "Mac"},
    )


def test_sending_from_the_tile_or_share_sheet(phone: Listener, make_node: NodeFactory) -> None:
    assert android._node is not None
    assert json.loads(android.sync_copy("hello"))["reason"] == "off"
    android._node.mesh.sync.set_policy(enabled=True)
    assert json.loads(android.sync_copy("hello", True))["reason"] == "no_devices"

    mac = make_node("Mac")
    owner(mac)("PUT", "/api/sync/policy", {"enabled": True})
    code = owner(mac).state()["pairing"]["code"]
    android._node.mesh.pair_with(code, f"127.0.0.1:{mac.port}")
    got: list[str] = []
    mac.mesh.sync.set_writer(lambda text, info: got.append(text) or True)

    result = json.loads(android.sync_copy("from the tile", True))
    assert result["status"] == "sent"
    assert result["names"]["delivered"] == ["Mac"]
    assert got == ["from the tile"]

    private = json.loads(android.sync_copy("password: hunter22", True))
    assert private["status"] == "error"
    assert private["reason"] == "sensitive"
    assert json.loads(android.sync_copy("password: hunter22", True, True))["status"] == "sent"


def test_nothing_happens_when_stopped() -> None:
    assert android._node is None
    assert json.loads(android.sync_copy("x"))["reason"] == "stopped"
    assert json.loads(android.sync_state())["active"] is False
