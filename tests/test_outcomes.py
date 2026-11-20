"""Every way a transfer can end, made for real: what Transfers shows, what History
keeps and each recipient's result agree, a result that isn't "done" says why,
and nothing that didn't arrive is ever recorded as received."""

from __future__ import annotations

import time
from typing import Any

from open_transfer.mesh import OFFER_TTL
from open_transfer.node import Node
from tests.conftest import NodeFactory
from tests.test_mesh import connect, incoming, owner, send, target_states, upload, wait_for


def entry(node: Node, title: str, direction: str = "sent") -> dict[str, Any]:
    def find() -> dict[str, Any] | None:
        for e in owner(node)("GET", "/api/history")[1]["entries"]:
            if e["title"] == title and e["direction"] == direction:
                return e  # type: ignore[no-any-return]
        return None

    return wait_for(find, timeout=15)  # type: ignore[no-any-return]


def agree(node: Node, job: str, title: str) -> dict[str, Any]:
    """History and the live view of ``job`` say the same, recipient by recipient."""
    kept = entry(node, title)  # written once the transfer is over…
    live = target_states(owner(node), job)  # …so the live view has settled too
    assert {d["name"]: d["status"] for d in kept["devices"]} == live
    for d in kept["devices"]:
        if d["status"] != "done":
            assert d.get("reason"), f"{d['name']} {d['status']} without a reason"
    return kept


def apps(make_node: NodeFactory, *names: str) -> list[Node]:
    nodes = [make_node(n) for n in ("Alpha", *names)]
    for other in nodes[1:]:
        connect(nodes[0], other)
    return nodes


def answer(node: Node, accept: bool) -> dict[str, Any]:
    offer = incoming(owner(node))
    owner(node)("POST", f"/api/incoming/{offer['id']}/{'accept' if accept else 'decline'}")
    return offer


def test_completed(make_node: NodeFactory) -> None:
    a, b = apps(make_node, "Bravo")
    job = send(owner(a), [b.mesh.id], [("done.txt", b"d")])
    answer(b, True)
    wait_for(lambda: target_states(owner(a), job) == {"Bravo": "accepted"})
    upload(owner(a), job, 0, b"d")
    assert agree(a, job, "done.txt")["status"] == "done"
    assert entry(b, "done.txt", "received")["status"] == "done"


def test_partly_delivered(make_node: NodeFactory) -> None:
    a, b, c = apps(make_node, "Bravo", "Charlie")
    job = send(owner(a), [b.mesh.id, c.mesh.id], [("part.txt", b"p")])
    answer(b, True)
    answer(c, False)
    wait_for(lambda: target_states(owner(a), job) == {"Bravo": "accepted", "Charlie": "declined"})
    upload(owner(a), job, 0, b"p")
    assert agree(a, job, "part.txt")["status"] == "partial"


def test_declined(make_node: NodeFactory) -> None:
    a, b = apps(make_node, "Bravo")
    job = send(owner(a), [b.mesh.id], [("no.txt", b"n")])
    answer(b, False)
    assert agree(a, job, "no.txt")["status"] == "declined"
    assert entry(b, "no.txt", "received")["status"] == "declined"


def test_expired(make_node: NodeFactory) -> None:
    a, b = apps(make_node, "Bravo")
    job = send(owner(a), [b.mesh.id], [("late.txt", b"l")])
    incoming(owner(b))
    b.mesh._expire_sessions(now=time.monotonic() + OFFER_TTL + 1)  # nobody answered
    assert agree(a, job, "late.txt")["status"] == "expired"
    assert entry(b, "late.txt", "received")["status"] == "expired"


def test_canceled_by_the_sender(make_node: NodeFactory) -> None:
    a, b = apps(make_node, "Bravo")
    job = send(owner(a), [b.mesh.id], [("stop.txt", b"s")])
    incoming(owner(b))
    owner(a)("DELETE", f"/api/send/{job}")
    kept = entry(a, "stop.txt")
    assert kept["status"] == "canceled"
    assert target_states(owner(a), job) == {"Bravo": "canceled"}
    assert entry(b, "stop.txt", "received")["status"] == "canceled"


def test_failed_when_the_device_cant_be_reached(make_node: NodeFactory) -> None:
    a, b = apps(make_node, "Bravo")
    b.stop()
    job = send(owner(a), [b.mesh.id], [("lost.txt", b"x")])
    kept = agree(a, job, "lost.txt")
    assert kept["status"] == "failed"


def test_disconnected_partway(make_node: NodeFactory) -> None:
    a, b = apps(make_node, "Bravo")
    job = send(owner(a), [b.mesh.id], [("cut.bin", b"c" * 200_000)])
    answer(b, True)
    wait_for(lambda: target_states(owner(a), job) == {"Bravo": "accepted"})
    b.stop()  # the receiving app went away
    upload(owner(a), job, 0, b"c" * 200_000)
    kept = agree(a, job, "cut.bin")
    assert kept["status"] == "failed"
    [received] = [e for e in b.mesh.history.query() if e["title"] == "cut.bin"]  # it's stopped
    assert received["status"] != "done"  # never recorded as received


def test_a_group_where_one_recipient_drops_out_partway(make_node: NodeFactory) -> None:
    a, b, c = apps(make_node, "Bravo", "Charlie")
    payload = b"g" * 300_000
    job = send(owner(a), [b.mesh.id, c.mesh.id], [("group.bin", payload)])
    answer(b, True)
    answer(c, True)
    wait_for(lambda: set(target_states(owner(a), job).values()) == {"accepted"})
    c.stop()
    upload(owner(a), job, 0, payload)
    kept = agree(a, job, "group.bin")
    assert kept["status"] == "partial"
    assert {d["name"]: d["status"] for d in kept["devices"]} == {
        "Bravo": "done",
        "Charlie": "failed",
    }
    assert entry(b, "group.bin", "received")["status"] == "done"
