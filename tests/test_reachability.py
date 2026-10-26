"""A device we hear on the Wi-Fi but can't connect to — almost always its firewall
(Windows with the network set to Public, or "Private networks" not allowed). The
app says so, by name, instead of "No device is showing that code"."""

from __future__ import annotations

from typing import Any

import pytest

from open_transfer.mesh import MeshError
from tests.conftest import NodeFactory
from tests.test_mesh import owner

WINDOWS = {
    "id": "d-" + "cd" * 12,
    "name": "Sonu-PC",
    "port": 4242,
    "form": "computer",
    "platform": "windows",
}


class Discovery:
    """Multicast that works one way: we hear announcements, nobody answers our query."""

    working = True

    def find(self, hint: str) -> None:
        pass


def blocked(*_args: Any, **_kwargs: Any) -> tuple[int, dict[str, Any]]:
    raise ConnectionRefusedError("blocked by its firewall")


def heard_but_blocked(make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    mac = make_node("Mac")
    peer = mac.mesh._upsert_peer(WINDOWS, "192.168.1.37")
    assert peer is not None
    monkeypatch.setattr(mac.mesh, "_http", blocked)
    assert mac.mesh._hello(peer) is False
    return mac


def entry(node, device_id: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return next(d for d in owner(node).state()["devices"] if d["id"] == device_id)


def test_a_device_that_does_not_answer_is_marked(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    mac = heard_but_blocked(make_node, monkeypatch)
    device = entry(mac, WINDOWS["id"])
    assert device["online"] is True  # we do hear it
    assert device["reachable"] is False


def test_it_recovers_once_it_answers(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    mac = heard_but_blocked(make_node, monkeypatch)
    monkeypatch.setattr(mac.mesh, "_http", lambda *a, **k: (200, WINDOWS))
    assert mac.mesh._hello(mac.mesh._peers[WINDOWS["id"]]) is True
    assert entry(mac, WINDOWS["id"])["reachable"] is True


def test_devices_that_answer_are_reachable(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    a.mesh.connect(f"127.0.0.1:{b.port}")
    assert entry(a, b.mesh.id)["reachable"] is True


def test_pairing_names_the_device_its_firewall_blocks(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    mac = heard_but_blocked(make_node, monkeypatch)
    mac.mesh.discovery = Discovery()  # type: ignore[assignment]
    with pytest.raises(MeshError) as caught:
        mac.mesh.pair_with("822328")
    error = caught.value
    assert error.code == "code_not_found"  # the page still opens the address field
    message = str(error)
    assert "Sonu-PC" in message
    assert "firewall" in message
    assert "Private" in message


def test_pairing_without_such_a_device_keeps_the_plain_message(make_node: NodeFactory) -> None:
    mac = make_node("Mac")
    mac.mesh.discovery = Discovery()  # type: ignore[assignment]
    with pytest.raises(MeshError, match="No device on this network is showing that code"):
        mac.mesh.pair_with("822328")
