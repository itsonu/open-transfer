"""Trusted Sync Network over real HTTP between app processes: pairing,
introductions, encrypted delivery, revocation and the owner's settings API."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from open_transfer.mesh import P2P
from open_transfer.node import Node
from tests.conftest import NodeFactory
from tests.test_mesh import Client, owner, wait_for


class Clip:
    """Stands in for the native agent on one node."""

    def __init__(self, node: Node) -> None:
        self.writes: list[str] = []
        node.mesh.sync.set_writer(self.write)

    def write(self, text: str, info: dict[str, Any]) -> bool:
        self.writes.append(text)
        return True


def pair(a: Node, b: Node) -> None:
    code = owner(b).state()["pairing"]["code"]
    status, data = owner(a)("POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{b.port}"})
    assert status == 200, data


def sync_on(node: Node, **policy: Any) -> None:
    status, data = owner(node)("PUT", "/api/sync/policy", {"enabled": True, **policy})
    assert status == 200, data


def trusts(a: Node, b: Node) -> bool:
    return a.mesh.trust.get(b.mesh.id) is not None


@pytest.fixture
def trio(make_node: NodeFactory) -> tuple[Node, Node, Node]:
    mac, win, pixel = make_node("Mac"), make_node("Windows"), make_node("Pixel")
    for node in (mac, win, pixel):
        sync_on(node)
    pair(mac, win)
    pair(mac, pixel)
    # The Mac introduced Windows and the Pixel, and told each where to find the other.
    wait_for(lambda: trusts(win, pixel) and trusts(pixel, win))
    wait_for(lambda: win.mesh._online_ids() >= {pixel.mesh.id})
    return mac, win, pixel


def test_a_copy_reaches_every_device_in_the_network(trio: tuple[Node, Node, Node]) -> None:
    mac, win, pixel = trio
    clips = {n.mesh.identity.name: Clip(n) for n in trio}
    result = win.mesh.sync.copy("hello from Windows")
    assert sorted(result["delivered"]) == sorted([mac.mesh.id, pixel.mesh.id])
    assert clips["Mac"].writes == clips["Pixel"].writes == ["hello from Windows"]
    assert clips["Windows"].writes == []


def test_the_settings_api(trio: tuple[Node, Node, Node]) -> None:
    mac, win, pixel = trio
    alice = owner(mac)
    status, data = alice("GET", "/api/sync")
    assert status == 200
    assert data["active"] is True
    assert {d["name"] for d in data["devices"]} == {"Windows", "Pixel"}

    status, data = alice("PUT", "/api/sync/policy", {"mode": "sometimes"})
    assert status == 400
    assert data["error"]["code"] == "bad_request"
    status, data = alice("PUT", "/api/sync/policy", {"colour": "red"})
    assert status == 400

    status, data = alice("PUT", f"/api/sync/devices/{pixel.mesh.id}", {"excluded": True})
    assert status == 200
    assert next(d for d in data["devices"] if d["name"] == "Pixel")["syncing"] is False
    status, data = alice("PUT", f"/api/sync/devices/{win.mesh.id}", {"pause_minutes": 30})
    assert next(d for d in data["devices"] if d["name"] == "Windows")["paused_until"] > time.time()

    status, data = alice("POST", "/api/sync/pause", {"minutes": None})
    assert data["paused"] is True
    status, data = alice("POST", "/api/sync/resume", {})
    assert data["paused"] is False


def test_sending_by_hand_and_sensitive_text(trio: tuple[Node, Node, Node]) -> None:
    mac, win, _pixel = trio
    clip = Clip(win)
    alice = owner(mac)
    status, data = alice("POST", "/api/sync/send", {"text": "password: hunter22"})
    assert status == 409
    assert data["error"]["code"] == "sensitive"
    status, data = alice("POST", "/api/sync/send", {"text": "password: hunter22", "confirm": True})
    assert status == 200
    assert win.mesh.id in data["delivered"]
    assert clip.writes == ["password: hunter22"]


def test_revoking_a_device_reaches_the_whole_network(trio: tuple[Node, Node, Node]) -> None:
    mac, win, pixel = trio
    status, _ = owner(mac)("DELETE", f"/api/sync/devices/{pixel.mesh.id}")
    assert status == 200
    assert not trusts(mac, pixel)
    wait_for(lambda: not trusts(win, pixel))
    clip = Clip(pixel)
    win.mesh.sync.copy("not for the Pixel")
    assert clip.writes == []


def test_unsigned_or_forged_sync_requests_are_refused(trio: tuple[Node, Node, Node]) -> None:
    mac, win, _pixel = trio
    stranger = Client(win.local_url)
    status, _data = stranger("POST", f"{P2P}/sync", {"env": {}}, headers={"X-OT-From": mac.mesh.id})
    assert status == 403
    # A real envelope, replayed without the sender's signature.
    captured: list[dict[str, Any]] = []
    real_post = mac.mesh._sync_post

    def spy(dst: str, kind: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        captured.append(body)
        return real_post(dst, kind, body)

    mac.mesh.sync._post = spy
    mac.mesh.sync.copy("watch the wire")
    assert captured
    assert all("watch the wire" not in json.dumps(b) for b in captured)
    status, _ = stranger("POST", f"{P2P}/sync", captured[0], headers={"X-OT-From": mac.mesh.id})
    assert status == 403


def test_visitors_cant_see_or_change_sync(make_node: NodeFactory) -> None:
    a = make_node("Alpha", owner_loopback=False)
    visitor = Client(a.local_url)
    assert visitor("GET", "/api/sync")[0] == 403
    assert visitor("PUT", "/api/sync/policy", {"enabled": True})[0] == 403
    assert visitor("POST", "/api/sync/send", {"text": "hi"})[0] == 403
    assert a.mesh.sync.active() is False


def test_paired_apps_sync_through_a_pin(make_node: NodeFactory) -> None:
    a = make_node("A")
    # B wants a PIN from everyone, loopback included: only signatures get A's clips in.
    b = make_node("B", pin="1357", owner_loopback=False)
    sync_on(a)
    b.mesh.sync.set_policy(enabled=True)
    status, data = owner(a)(
        "POST", "/api/pair", {"code": b.mesh.pair_code, "address": f"127.0.0.1:{b.port}"}
    )
    assert status == 200, data
    clip = Clip(b)
    result = a.mesh.sync.copy("through the PIN")
    assert result["delivered"] == [b.mesh.id]
    assert clip.writes == ["through the PIN"]


def test_queued_clips_arrive_when_a_device_comes_back(make_node: NodeFactory) -> None:
    a, b = make_node("A"), make_node("B")
    for node in (a, b):
        sync_on(node)
    pair(a, b)
    clip = Clip(b)
    with a.mesh._lock:
        a.mesh._peers[b.mesh.id].last_seen = 0.0  # looks offline
    assert a.mesh.sync.copy("wait for me")["queued"] == [b.mesh.id]
    a.mesh.connect(f"127.0.0.1:{b.port}")  # back: delivered without another copy
    wait_for(lambda: clip.writes == ["wait for me"])
