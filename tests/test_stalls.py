"""Transfers that stop moving end with a clear reason; long ones that keep moving don't.

A stall is measured from the last byte that arrived, not from when the offer was
made: a 10 GB video at 2 MB/s takes well over an hour and is still healthy.
"""

from __future__ import annotations

import time

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
        id=f"s-{state}-{created}",
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
