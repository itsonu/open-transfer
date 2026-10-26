"""All ten device pairs, both ways, plus fan-out across a whole network.

Every platform takes part the way its OS allows (docs/sync.md):

- **Mac / Windows:** the native agent notices the copy and writes what arrives.
- **Android phone / tablet:** clips arrive in the background; sending is one
  tap (the Quick Settings tile, which hands the text to the engine by hand).
- **iPhone / iPad:** the "Send clipboard" and "Paste from my devices"
  Shortcuts, through a hub.

Each runs a real app process (an HTTP server per device). Only the OS clipboard
is a stand-in; the agents themselves are tested in tests/test_clipagent.py and
on real devices (docs/device-testing.md).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

import pytest

from open_transfer.node import Node
from tests.conftest import NodeFactory
from tests.test_clipagent import FakeAgent
from tests.test_mesh import owner, wait_for
from tests.test_sync_clients import Shortcut, enroll

PLATFORMS = ("mac", "windows", "android", "tablet", "ios")
NAMES = {
    "mac": "Mac",
    "windows": "Windows PC",
    "android": "Pixel",
    "tablet": "Galaxy Tab",
    "ios": "iPhone",
}
#: The ten pairs from the spec (tablet = an Android tablet; iPad is covered by "ios").
PAIRS = [
    ("mac", "windows"),
    ("mac", "ios"),
    ("mac", "android"),
    ("mac", "tablet"),
    ("windows", "ios"),
    ("windows", "android"),
    ("windows", "tablet"),
    ("android", "ios"),
    ("ios", "tablet"),
    ("tablet", "android"),
]


@dataclass
class Device:
    platform: str
    name: str
    node: Node | None = None  # an app (computer, phone, tablet)
    agent: FakeAgent | None = None  # desktop: the OS clipboard
    writes: list[str] = field(default_factory=list)  # Android: what reached the clipboard
    shortcut: Shortcut | None = None  # iPhone/iPad: its two Shortcuts

    def send(self, text: str) -> None:
        """The least the OS asks of the owner to send ``text``."""
        if self.agent is not None:
            self.agent.user_copies(text)  # just copy: the agent does the rest
        elif self.shortcut is not None:
            status, data = self.shortcut.push(text)  # run "Send clipboard"
            assert status == 200, data
        else:
            assert self.node is not None
            self.node.mesh.sync.copy(text, manual=True)  # tap the tile

    def has(self, text: str) -> bool:
        """Whether ``text`` is ready to paste (an iPhone runs "Paste from my devices")."""
        if self.agent is not None:
            return self.agent.text == text
        if self.shortcut is not None:
            return bool(self.shortcut.latest()[1]["text"] == text)
        return bool(self.writes) and self.writes[-1] == text


def start_app(make_node: NodeFactory, platform: str, name: str) -> Device:
    node = make_node(name)
    status, data = owner(node)("PUT", "/api/sync/policy", {"enabled": True})
    assert status == 200, data
    device = Device(platform, name, node)
    if platform in ("mac", "windows"):
        device.agent = FakeAgent()
        node.start_clipboard(device.agent).interval = 0.02
    else:  # Android: the service writes the clipboard (see android.start)
        node.mesh.sync.set_writer(lambda text, info: device.writes.append(text) or True)
    return device


def pair_all(apps: list[Device]) -> None:
    """Pair each app with the first one only; introductions do the rest."""
    first = apps[0].node
    assert first is not None
    for other in apps[1:]:
        assert other.node is not None
        code = owner(other.node).state()["pairing"]["code"]
        status, data = owner(first)(
            "POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{other.node.port}"}
        )
        assert status == 200, data
    nodes = [a.node for a in apps if a.node is not None]
    for a, b in itertools.permutations(nodes, 2):
        wait_for(lambda a=a, b=b: a.mesh.trust.get(b.mesh.id) is not None)
        wait_for(lambda a=a, b=b: b.mesh.id in a.mesh._online_ids())


def build(
    make_node: NodeFactory, platforms: list[str], hub_for_ios: str | None = None
) -> dict[str, Device]:
    devices: dict[str, Device] = {}
    for platform in platforms:
        if platform not in ("ios", "ipad"):
            devices[platform] = start_app(make_node, platform, NAMES.get(platform, platform))
    if not devices:  # two Apple devices: any app in between is their hub
        devices["hub"] = start_app(make_node, "android", "Hub")
    apps = list(devices.values())
    if len(apps) > 1:
        pair_all(apps)
    hub = devices[hub_for_ios] if hub_for_ios else apps[0]
    assert hub.node is not None
    for platform in platforms:
        if platform in ("ios", "ipad"):
            name = "iPad" if platform == "ipad" else "iPhone"
            _, shortcut = enroll(hub.node, name)
            devices[platform] = Device(platform, name, shortcut=shortcut)
    return devices


@pytest.mark.parametrize(("a", "b"), PAIRS, ids=[f"{a}-{b}" for a, b in PAIRS])
def test_each_pair_both_ways(make_node: NodeFactory, a: str, b: str) -> None:
    devices = build(make_node, [a, b])
    first, second = devices[a], devices[b]
    first.send(f"from {first.name}")
    wait_for(lambda: second.has(f"from {first.name}"), timeout=10)
    second.send(f"from {second.name}")
    wait_for(lambda: first.has(f"from {second.name}"), timeout=10)


def test_iphone_and_ipad_share_a_hub(make_node: NodeFactory) -> None:
    devices = build(make_node, ["ios", "ipad"])
    devices["ios"].send("to the iPad")
    assert devices["ipad"].has("to the iPad")
    devices["ipad"].send("back to the iPhone")
    assert devices["ios"].has("back to the iPhone")
    assert devices["hub"].writes == ["to the iPad", "back to the iPhone"]


def test_one_copy_reaches_every_device_in_a_network(make_node: NodeFactory) -> None:
    devices = build(make_node, ["mac", "windows", "android", "tablet", "ios", "ipad"])
    names = list(devices)
    for sender in names:
        text = f"everyone, from {devices[sender].name}"
        devices[sender].send(text)
        for receiver in names:
            if receiver != sender:
                wait_for(lambda r=receiver, t=text: devices[r].has(t), timeout=10)


def test_a_removed_device_gets_nothing_more(make_node: NodeFactory) -> None:
    devices = build(make_node, ["mac", "windows", "android"])
    mac, win, pixel = devices["mac"], devices["windows"], devices["android"]
    assert mac.node is not None
    assert pixel.node is not None
    assert win.node is not None
    status, _ = owner(mac.node)("DELETE", f"/api/sync/devices/{pixel.node.mesh.id}")
    assert status == 200
    wait_for(lambda: win.node is not None and win.node.mesh.trust.get(pixel.node.mesh.id) is None)  # type: ignore[union-attr]
    win.send("after the Pixel left")
    wait_for(lambda: mac.has("after the Pixel left"))
    assert "after the Pixel left" not in pixel.writes
    pixel.send("still trying")
    assert not mac.has("still trying")
    assert not win.has("still trying")


def test_a_device_that_was_away_gets_only_fresh_clips(make_node: NodeFactory) -> None:
    devices = build(make_node, ["mac", "android"])
    mac, pixel = devices["mac"], devices["android"]
    assert mac.node is not None
    assert pixel.node is not None
    mac.node.mesh.sync.set_policy(expiry=30)
    away: dict[str, Any] = {}
    with mac.node.mesh._lock:
        away["peer"] = mac.node.mesh._peers[pixel.node.mesh.id]
        away["peer"].last_seen = 0.0  # looks away
    mac.send("stale soon")
    wait_for(lambda: mac.node is not None and mac.node.mesh.sync.status()["pending"] == 1)  # type: ignore[union-attr]
    # Time passes beyond the expiry while it's away…
    pending = mac.node.mesh.sync._pending[pixel.node.mesh.id]
    pending.expires = 0.0
    mac.node.mesh.connect(f"127.0.0.1:{pixel.node.port}")  # …then it's back
    mac.node.mesh.sync.tick()
    assert "stale soon" not in pixel.writes
    mac.send("fresh")
    wait_for(lambda: pixel.has("fresh"))
