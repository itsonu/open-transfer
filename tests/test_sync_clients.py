"""iPhones and iPads in the sync network through Shortcuts: enrolling one on a
hub, sending its clipboard, pasting the newest clip, and removing it."""

from __future__ import annotations

import time
from typing import Any

import pytest

from open_transfer.node import Node
from tests.conftest import NodeFactory
from tests.test_mesh import Client, owner, wait_for


class Clip:
    def __init__(self, node: Node) -> None:
        self.writes: list[tuple[str, dict[str, Any]]] = []
        node.mesh.sync.set_writer(lambda text, info: self.writes.append((text, info)) or True)

    @property
    def texts(self) -> list[str]:
        return [t for t, _ in self.writes]


class Shortcut:
    """What the iPhone's Shortcuts do: Get Contents of URL with a bearer token."""

    def __init__(self, node: Node, token: str) -> None:
        self.http = Client(node.local_url)
        self.token = token

    def push(self, text: str, **extra: Any) -> tuple[int, Any]:
        return self.http(
            "POST", "/api/sync/client/push", {"text": text, **extra}, headers=self.auth
        )

    def latest(self) -> tuple[int, Any]:
        return self.http("GET", "/api/sync/client/latest", headers=self.auth)

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def enroll(hub: Node, name: str = "Sam’s iPhone") -> tuple[str, Shortcut]:
    status, data = owner(hub)("POST", "/api/sync/clients", {"name": name})
    assert status == 200, data
    assert data["push_url"].endswith("/api/sync/client/push")
    assert data["latest_url"].endswith("/api/sync/client/latest")
    return data["client"]["id"], Shortcut(hub, data["token"])


@pytest.fixture
def network(make_node: NodeFactory) -> tuple[Node, Node]:
    mac, win = make_node("Mac"), make_node("Windows")
    for node in (mac, win):
        status, _ = owner(node)("PUT", "/api/sync/policy", {"enabled": True})
        assert status == 200
    code = owner(win)("POST", "/api/pair/open")[1]["code"]
    status, data = owner(mac)(
        "POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{win.port}"}
    )
    assert status == 200, data
    return mac, win


def test_an_iphone_sends_its_clipboard_through_its_hub(network: tuple[Node, Node]) -> None:
    mac, win = network
    mac_clip, win_clip = Clip(mac), Clip(win)
    _, iphone = enroll(mac)
    status, data = iphone.push("from the iPhone")
    assert status == 200, data
    assert data["message"] == "Sent to Mac, Windows"
    assert mac_clip.texts == win_clip.texts == ["from the iPhone"]
    assert win_clip.writes[0][1]["from"] == "Sam’s iPhone"
    # Its own clip doesn't come back to it.
    assert iphone.latest()[1]["text"] == ""


def test_an_iphone_pastes_the_newest_clip_from_any_device(network: tuple[Node, Node]) -> None:
    mac, win = network
    Clip(mac)
    _, iphone = enroll(mac)
    assert iphone.latest()[1] == {"text": "", "from": "", "message": "Nothing new to paste"}
    win.mesh.sync.copy("copied on Windows")
    status, data = iphone.latest()
    assert status == 200
    assert data == {
        "text": "copied on Windows",
        "from": "Windows",
        "message": "Pasted from Windows",
    }
    time.sleep(0.01)
    mac.mesh.sync.copy("copied on the Mac")
    assert iphone.latest()[1]["text"] == "copied on the Mac"
    assert iphone.latest()[1]["text"] == "copied on the Mac"  # still there until it expires


def test_two_apple_devices_on_one_hub(network: tuple[Node, Node]) -> None:
    mac, _ = network
    Clip(mac)
    _, iphone = enroll(mac, "iPhone")
    _, ipad = enroll(mac, "iPad")
    iphone.push("to the iPad")
    assert ipad.latest()[1]["text"] == "to the iPad"
    assert ipad.latest()[1]["from"] == "iPhone"


def test_private_text_asks_first(network: tuple[Node, Node]) -> None:
    mac, win = network
    win_clip = Clip(win)
    _, iphone = enroll(mac)
    status, data = iphone.push("password: hunter22")
    assert status == 409
    assert data["error"]["code"] == "sensitive"
    assert "private" in data["message"]
    assert iphone.push("password: hunter22", confirm=True)[0] == 200
    assert win_clip.writes[-1][1]["sensitive"] is True


def test_plain_text_bodies_work_too(network: tuple[Node, Node]) -> None:
    mac, win = network
    win_clip = Clip(win)
    _, iphone = enroll(mac)
    status, _ = iphone.http(
        "POST",
        "/api/sync/client/push",
        data=b"just the text",
        headers={**iphone.auth, "Content-Type": "text/plain"},
    )
    assert status == 200
    assert win_clip.texts == ["just the text"]


def test_tokens_and_removal(network: tuple[Node, Node]) -> None:
    mac, _ = network
    client_id, iphone = enroll(mac)
    _, data = owner(mac)("GET", "/api/sync/clients")
    assert [c["name"] for c in data["clients"]] == ["Sam’s iPhone"]
    assert "token" not in data["clients"][0]
    assert iphone.token not in (mac.config.state_path / "sync-clients.json").read_text()
    assert Shortcut(mac, "wrong").latest()[0] == 401
    assert Client(mac.local_url)("GET", "/api/sync/client/latest")[0] == 401
    _status, data = owner(mac)("GET", "/api/sync")
    assert [c["name"] for c in data["clients"]] == ["Sam’s iPhone"]
    assert owner(mac)("DELETE", f"/api/sync/clients/{client_id}")[0] == 200
    assert iphone.latest()[0] == 401
    assert iphone.push("still here?")[0] == 401


def test_guessing_tokens_is_slowed_down(network: tuple[Node, Node]) -> None:
    mac, _ = network
    _, iphone = enroll(mac)
    codes = [Shortcut(mac, f"guess-{i}").latest()[0] for i in range(12)]
    assert codes[:10] == [401] * 10
    assert codes[-1] == 429
    assert iphone.latest()[0] == 429  # same address: wait a minute


def test_pausing_or_leaving_out_an_iphone(network: tuple[Node, Node]) -> None:
    mac, win = network
    client_id, iphone = enroll(mac)
    status, _ = owner(mac)("PUT", f"/api/sync/devices/{client_id}", {"pause_minutes": 60})
    assert status == 200
    assert iphone.push("paused?")[1]["message"] == "This iPhone or iPad is paused or left out"
    win.mesh.sync.copy("not for a paused iPhone")
    assert iphone.latest()[1]["text"] == ""


def test_sync_off_on_the_hub(make_node: NodeFactory) -> None:
    hub = make_node("Hub")
    _, iphone = enroll(hub)
    assert iphone.push("hello")[1]["message"] == "Clipboard sync is off on Hub"


def test_shortcuts_get_past_a_pin(make_node: NodeFactory) -> None:
    hub = make_node("Hub", pin="2468", owner_loopback=False)
    hub.mesh.sync.set_policy(enabled=True)
    assert hub.mesh.sync.clients is not None
    _, token = hub.mesh.sync.clients.add("iPad")
    Clip(hub)
    status, data = Shortcut(hub, token).push("through the PIN")
    assert status == 200, data
    assert data["message"] == "Sent to Hub"


def test_queued_for_a_member_thats_away(network: tuple[Node, Node]) -> None:
    mac, win = network
    win_clip = Clip(win)
    _, iphone = enroll(mac)
    with mac.mesh._lock:
        mac.mesh._peers[win.mesh.id].last_seen = 0.0
    status, data = iphone.push("later")
    assert status == 200
    assert data["queued"] == [win.mesh.id]
    mac.mesh.connect(f"127.0.0.1:{win.port}")
    wait_for(lambda: win_clip.texts == ["later"])
