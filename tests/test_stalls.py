"""Transfers that stop moving end with a clear reason; long ones that keep moving don't.

A stall is measured from the last byte that arrived, not from when the offer was
made: a 10 GB video at 2 MB/s takes well over an hour and is still healthy.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from open_transfer.mesh import (
    ACCEPTED,
    EXPIRED,
    FAILED,
    OFFER_TTL,
    PENDING,
    RECEIVE_IDLE,
    RECEIVING,
    IncomingSession,
)
from tests.conftest import NodeFactory


def session(node, state: str, created: float, active: float = 0.0) -> IncomingSession:  # type: ignore[no-untyped-def]
    s = IncomingSession(
        id=f"s-{secrets.token_hex(6)}",
        secret="x",
        origin={"name": "Studio Mac"},
        target=node.mesh.id,
        files=[],
        paired=False,
        created=created,
        state=state,
        active=active,
    )
    node.mesh._incoming[s.id] = s
    return s


def test_a_long_transfer_that_keeps_moving_is_never_cut_off(make_node: NodeFactory) -> None:
    node = make_node("Tab")
    now = time.monotonic()  # the app's own housekeeping runs on this clock too
    busy = session(node, RECEIVING, created=now - 7_200, active=now)  # two hours in, a byte now
    node.mesh._expire_sessions(now=now + 10)
    assert busy.state == RECEIVING


def test_a_transfer_that_stops_moving_fails_and_says_who(make_node: NodeFactory) -> None:
    node = make_node("Tab")
    now = time.monotonic()
    quiet = session(node, RECEIVING, created=now - 3_000, active=now - RECEIVE_IDLE - 1)
    node.mesh._expire_sessions(now=now)
    assert quiet.state == FAILED
    assert quiet.reason == "Studio Mac stopped sending."


def test_accepted_but_nothing_sent_says_the_sender_stopped(make_node: NodeFactory) -> None:
    node = make_node("Tab")
    now = time.monotonic()
    accepted = session(node, ACCEPTED, created=now - OFFER_TTL * 2 - 1)
    node.mesh._expire_sessions(now=now)
    assert accepted.state == EXPIRED
    assert accepted.reason == "Studio Mac didn’t start sending."  # it was answered


def test_an_unanswered_offer_still_says_no_answer(make_node: NodeFactory) -> None:
    node = make_node("Tab")
    now = time.monotonic()
    pending = session(node, PENDING, created=now - OFFER_TTL - 1)
    node.mesh._expire_sessions(now=now)
    assert (pending.state, pending.reason) == (EXPIRED, "No answer")


# ------------------------------------------------- the sender's window closed


def accepted_but_never_sent(make_node: NodeFactory) -> tuple[Any, Any, str]:
    """Alpha's page offers a file and Bravo accepts; then the page is gone (nothing uploads)."""
    from tests.test_mesh import connect, incoming, owner, send, target_states, wait_for

    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    job = send(owner(a), [b.mesh.id], [("trip.mov", b"x" * 1000)])
    offer = incoming(owner(b))
    owner(b)("POST", f"/api/incoming/{offer['id']}/accept")
    wait_for(lambda: target_states(owner(a), job) == {"Bravo": "accepted"})
    return a, b, job


def test_a_send_whose_window_closed_before_any_byte_fails_and_says_why(
    make_node: NodeFactory,
) -> None:
    from tests.test_mesh import owner, wait_for

    a, b, job = accepted_but_never_sent(make_node)
    a.mesh._expire_sessions(now=time.monotonic() + OFFER_TTL * 2 + 1)
    delivery = a.mesh._jobs[job].deliveries[b.mesh.id]
    assert delivery.view_state() == FAILED
    assert "Choose the files again" in delivery.reason
    assert a.mesh._jobs[job].finished  # no longer "in progress"
    # The receiver is told rather than left waiting.
    wait_for(
        lambda: all(s.state in {"canceled", FAILED, EXPIRED} for s in b.mesh._incoming.values())
    )

    def recorded() -> Any:
        return next((e for e in owner(a)("GET", "/api/history")[1]["entries"] if e["title"] == "trip.mov"), None)  # fmt: skip

    entry = wait_for(recorded)
    assert entry["status"] == FAILED  # History says what happened


def test_a_send_that_stopped_partway_fails_after_the_idle_time(make_node: NodeFactory) -> None:
    a, b, job = accepted_but_never_sent(make_node)
    sent = a.mesh._jobs[job]
    delivery = sent.deliveries[b.mesh.id]
    delivery.state, delivery.sent = "sending", 400  # partway, then the window closed
    sent.active = time.monotonic()
    a.mesh._expire_sessions(now=time.monotonic() + RECEIVE_IDLE - 5)
    assert delivery.view_state() == "sending"  # quiet for a while is fine
    a.mesh._expire_sessions(now=time.monotonic() + RECEIVE_IDLE + 1)
    assert delivery.view_state() == FAILED
    assert "partway" in delivery.reason


def test_a_send_that_keeps_moving_is_left_alone(make_node: NodeFactory) -> None:
    a, b, job = accepted_but_never_sent(make_node)
    sent = a.mesh._jobs[job]
    sent.created -= 7_200  # offered two hours ago…
    sent.active = time.monotonic()  # …and a byte just went out
    sent.deliveries[b.mesh.id].state = "sending"
    a.mesh._expire_sessions(now=time.monotonic() + 10)
    assert sent.deliveries[b.mesh.id].view_state() == "sending"


# ------------------------------------------------------- nothing left behind


def test_a_canceled_incoming_file_leaves_no_partial_data(make_node: NodeFactory) -> None:
    from open_transfer.mesh import CANCELED, FINISHED_KEEP, IncomingFile, _incoming_chunk_key

    node = make_node("Tab")
    s = session(node, CANCELED, created=time.monotonic() - 500)
    s.files = [IncomingFile("movie.mov", 10_000_000, "video/quicktime")]
    s.finished = time.monotonic()
    chunks = node.mesh.storage.chunks
    key = _incoming_chunk_key(s.id, 0)
    chunks.touch(key, "movie.mov", 10_000_000, 0)
    chunks.append(key, 0, b"x" * 1000)
    assert chunks.path(key) is not None
    node.mesh._expire_sessions(now=time.monotonic() + FINISHED_KEEP + 1)
    assert s.id not in node.mesh._incoming
    assert chunks.path(key) is None  # the sparse 10 MB staging file is gone


def test_a_dropped_send_leaves_no_staged_chunks(make_node: NodeFactory) -> None:
    from open_transfer.mesh import FINISHED_KEEP

    a, _b, job = accepted_but_never_sent(make_node)
    a.mesh._relay_chunks.touch("up-1", "trip.mov", 1000, 0)
    a.mesh._relay_chunks.append("up-1", 0, b"x" * 100)
    a.mesh._relay_meta["up-1"] = {"job_id": job, "index": 0, "total": 1000, "name": "trip.mov"}
    a.mesh._expire_sessions(now=time.monotonic() + OFFER_TTL * 2 + 1)  # fails, finishes
    a.mesh._expire_sessions(now=time.monotonic() + OFFER_TTL * 2 + FINISHED_KEEP + 2)
    assert job not in a.mesh._jobs
    assert a.mesh._relay_chunks.path("up-1") is None
    assert "up-1" not in a.mesh._relay_meta


# ------------------------------------------------- the app itself is closed


def history(node) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    return node.mesh.history.query()  # type: ignore[no-any-return]


def test_closing_the_sending_app_records_it_and_tells_the_receiver(
    make_node: NodeFactory,
) -> None:
    from tests.test_mesh import wait_for

    a, b, _job = accepted_but_never_sent(make_node)
    a.stop()  # the window closed (or Cmd+Q): the app shuts down
    [entry] = history(a)
    assert entry["status"] == "canceled"
    assert "Open Transfer was closed" in entry["devices"][0]["reason"]
    # Bravo hears it at once, instead of waiting for the idle limit.
    wait_for(lambda: [s.state for s in b.mesh._incoming.values()] == ["canceled"], timeout=5)


def test_closing_the_receiving_app_records_it(make_node: NodeFactory) -> None:
    _a, b, _job = accepted_but_never_sent(make_node)
    b.stop()
    [entry] = history(b)
    assert entry["direction"] == "received"
    assert entry["status"] != "done"  # never "received"
    assert "Open Transfer was closed" in entry["devices"][0]["reason"]
