"""A paired device's new address (N1): a phone's Wi-Fi lease changes, a laptop
reconnects or switches network. Anyone on the Wi-Fi can send an announcement
with a paired device's id, so for paired devices the address only changes on
proof: a hello the device signed, or its answer to a challenge sent there."""

from __future__ import annotations

import secrets
import time

from open_transfer.mesh import P2P, Peer
from open_transfer.node import Node
from tests.conftest import NodeFactory
from tests.test_mesh import lan_ip, owner, wait_for


def pair(a: Node, b: Node) -> None:
    code = owner(b)("POST", "/api/pair/open")[1]["code"]
    status, data = owner(a)("POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{b.port}"})
    assert status == 200, data


def announce(node: Node, about: Node, src: str, **changes: object) -> None:
    info = about.mesh.self_info(include_visitors=False)
    node.mesh._on_packet({"type": "announce", **info, **changes}, src)


def test_anyones_packet_doesnt_move_a_paired_device(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    pair(a, b)
    peer = a.mesh._peers[b.mesh.id]
    seen = peer.last_seen
    announce(a, b, "192.0.2.66", name="Evil")  # spoofed: nobody answers there
    time.sleep(0.5)
    assert peer.host == "127.0.0.1"
    assert peer.name == "Bravo"
    assert peer.last_seen == seen
    assert a.mesh.trust.get(b.mesh.id).name == "Bravo"  # type: ignore[union-attr]


def test_a_paired_device_that_really_moved_is_followed(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    pair(a, b)
    moved = lan_ip()
    announce(a, b, moved)  # its new lease: it answers the challenge there
    wait_for(lambda: a.mesh._peers[b.mesh.id].host == moved, timeout=5)


def test_a_signed_hello_moves_it_at_once(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    pair(a, b)
    moved = lan_ip()
    to_a = b.mesh._peers[a.mesh.id]
    to_a.host = moved  # Bravo now reaches Alpha over the LAN, so it arrives from there
    assert b.mesh._hello(to_a)
    assert a.mesh._peers[b.mesh.id].host == moved


def test_a_device_proves_only_its_own_address(make_node: NodeFactory) -> None:
    # Someone relaying our challenge to the real device gets no proof for their address.
    a, b = make_node("Alpha"), make_node("Bravo")
    pair(a, b)
    to_b = Peer(b.mesh.id, "", "computer", "unknown", "127.0.0.1", b.port)

    def challenge(to: str) -> dict[str, object]:
        body = {
            **a.mesh.self_info(include_visitors=False),
            "challenge": secrets.token_hex(16),
            "to": to,
        }
        status, reply = a.mesh._http(to_b, "POST", f"{P2P}/hello", body=body)
        assert status == 200
        return reply

    assert "proof" not in challenge(f"192.0.2.66:{b.port}")
    assert "proof" in challenge(f"127.0.0.1:{b.port}")


def test_unpaired_devices_move_as_before(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    a.mesh._upsert_peer(b.mesh.self_info(include_visitors=False), "192.168.1.50")
    announce(a, b, "192.168.1.77")
    assert a.mesh._peers[b.mesh.id].host == "192.168.1.77"
