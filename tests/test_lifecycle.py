"""The transfer lifecycle on real nodes: deadlines, reasons, reconciliation, cancels.

Deadlines are driven by calling ``mesh._check_deadlines(now=...)`` with a time in
the future, so nothing here waits for a real timeout.
"""

from __future__ import annotations

import http.client
import time
from pathlib import Path
from typing import Any

import pytest

from open_transfer import mesh as mesh_module
from open_transfer.lifecycle import REASONS, can_move, describe
from open_transfer.mesh import ACCEPT_TTL, OFFER_TTL, SENDER_GRACE, STALL_TIMEOUT, _Sink
from open_transfer.node import Node
from open_transfer.storage import InsufficientStorage
from tests import test_mesh
from tests.test_mesh import (
    Client,
    NodeFactory,
    connect,
    device_id,
    incoming,
    owner,
    send,
    upload,
    wait_for,
)

Json = dict[str, Any]
make_node = test_mesh.make_node  # the shared pytest fixture


def pair_up(make_node: NodeFactory) -> tuple[Node, Node, Client, Client]:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    return a, b, owner(a), owner(b)


def target(client: Client, job_id: str) -> Json:
    for job in client.state()["outgoing"]:
        if job["id"] == job_id:
            return job["targets"][0]  # type: ignore[no-any-return]
    raise AssertionError("job not found")


def session_of(client: Client, job_id: str | None = None) -> Json:
    items = client.state()["incoming"]
    assert items, "no incoming transfer"
    return items[-1]  # type: ignore[no-any-return]


def later(seconds: float) -> float:
    return time.monotonic() + seconds


def accept(bob: Client) -> Json:
    offer = incoming(bob)
    assert bob("POST", f"/api/incoming/{offer['id']}/accept")[0] == 200
    return offer


def accepted(alice: Client, job: str) -> None:
    wait_for(lambda: target(alice, job)["state"] == "accepted")


def history_item(client: Client, job: str, direction: str) -> Json:
    status, data = client("GET", "/api/history")
    assert status == 200
    return next(i for i in data["items"] if i["transfer_id"] == job and i["direction"] == direction)


# ------------------------------------------------------------- the model


def test_every_reason_has_a_message_and_an_action() -> None:
    required = {
        "receiver_declined", "insufficient_storage", "file_too_large", "sender_cancelled",
        "receiver_cancelled", "sender_disconnected", "receiver_disconnected", "network_timeout",
        "transfer_stalled", "app_restart", "app_closed", "expired", "invalid_request",
        "permission_denied", "destination_unavailable", "unknown_failure",
    }  # fmt: skip
    assert required <= set(REASONS)
    for code, (message, action) in REASONS.items():
        assert message.endswith("."), code
        assert action.endswith("."), code
    assert describe("no_such_code")["reason"] == REASONS["unknown_failure"][0]
    assert describe("expired", "Custom")["reason"] == "Custom"
    assert describe("") == {"reason_code": "", "reason": "", "action": ""}


@pytest.mark.parametrize(
    ("old", "new", "allowed"),
    [
        ("offered", "accepted", True),
        ("offered", "transferring", False),
        ("accepted", "transferring", True),
        ("accepted", "completed", True),
        ("transferring", "partial", True),
        ("transferring", "offered", False),
        ("completed", "cancelled", False),
        ("cancelled", "completed", False),
        ("failed", "completed", False),
        ("declined", "accepted", False),
        ("transferring", "transferring", True),
    ],
)
def test_allowed_transitions(old: str, new: str, allowed: bool) -> None:
    assert can_move(old, new) is allowed


# ------------------------------------------------------------- deadlines


def test_offer_expiry_is_deterministic_on_both_sides(make_node: NodeFactory) -> None:
    _a, b, alice, bob = pair_up(make_node)
    job = send(alice, [device_id(alice, "Bravo")], [("f.txt", b"hi")])
    incoming(bob)

    b.mesh._check_deadlines(later(OFFER_TTL + 1))
    assert session_of(bob)["state"] == "expired"
    assert session_of(bob)["reason_code"] == "expired"
    wait_for(lambda: target(alice, job)["state"] == "expired")
    assert target(alice, job)["reason_code"] == "expired"


def test_sender_expires_the_offer_even_if_it_never_heard_back(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flaky real-device expiry: the sender's watcher never ran (busy worker pool)."""
    a, b, alice, bob = pair_up(make_node)
    monkeypatch.setattr(a.mesh, "_follow_remote", lambda *args: None)
    job = send(alice, [device_id(alice, "Bravo")], [("f.txt", b"hi")])
    incoming(bob)
    b.stop()  # and the receiver is gone, so there is nobody to ask

    a.mesh._check_deadlines(later(OFFER_TTL + SENDER_GRACE + 1))
    assert target(alice, job)["state"] == "expired"
    assert history_item(alice, job, "sent")["state"] == "expired"


def test_accepted_without_a_first_byte_fails_on_both_sides(make_node: NodeFactory) -> None:
    a, b, alice, bob = pair_up(make_node)
    job = send(alice, [device_id(alice, "Bravo")], [("f.txt", b"hi")])
    accept(bob)
    accepted(alice, job)

    b.mesh._check_deadlines(later(ACCEPT_TTL + 1))
    assert session_of(bob)["state"] == "failed"
    assert session_of(bob)["reason_code"] == "sender_disconnected"
    a.mesh._check_deadlines(later(ACCEPT_TTL + 2))
    t = target(alice, job)
    assert (t["state"], t["reason_code"]) == ("failed", "sender_disconnected")
    assert history_item(bob, job, "received")["reason_code"] == "sender_disconnected"


def test_a_stalled_multi_file_transfer_ends_partial_on_both_sides(make_node: NodeFactory) -> None:
    """Before: file 2 was never sent, so the receiver stayed "receiving" forever."""
    a, b, alice, bob = pair_up(make_node)
    job = send(alice, [device_id(alice, "Bravo")], [("one.txt", b"1"), ("two.txt", b"2")])
    accept(bob)
    accepted(alice, job)
    assert upload(alice, job, 0, b"1")[0] == 200  # the second file never comes

    b.mesh._check_deadlines(later(STALL_TIMEOUT + 1))
    s = session_of(bob)
    assert (s["state"], s["reason_code"]) == ("partial", "transfer_stalled")
    assert [f["state"] for f in s["files"]] == ["done", "failed"]
    a.mesh._check_deadlines(later(STALL_TIMEOUT + 2))
    t = target(alice, job)
    assert (t["state"], t["reason_code"]) == ("partial", "transfer_stalled")
    sent = history_item(alice, job, "sent")
    assert sent["state"] == "partial"
    assert sent["recipients"][0]["files_done"] == [0]


# -------------------------------------------------------- reconciliation


def test_lost_final_acknowledgement_is_not_a_failure(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _a, b, alice, bob = pair_up(make_node)
    original = _Sink._put_remote

    def put_then_lose_the_reply(self: _Sink) -> None:
        original(self)  # the receiver saves the whole file …
        raise ConnectionResetError("reply lost")  # … but its answer never arrives

    monkeypatch.setattr(_Sink, "_put_remote", put_then_lose_the_reply)
    job = send(alice, [device_id(alice, "Bravo")], [("f.txt", b"hello")])
    accept(bob)
    accepted(alice, job)
    status, result = upload(alice, job, 0, b"hello")
    assert status == 200
    assert all(t["ok"] for t in result["targets"].values())
    assert target(alice, job)["state"] == "done"
    assert (Path(b.config.storage_dir) / "f.txt").read_bytes() == b"hello"
    assert history_item(alice, job, "sent")["state"] == "completed"


def test_receiver_failure_reason_reaches_the_sender(make_node: NodeFactory) -> None:
    _a, b, alice, bob = pair_up(make_node)
    original = b.mesh.storage.save_stream

    def full_disk(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "c.txt":
            raise InsufficientStorage("Disk full.")
        return original(name, *args, **kwargs)

    b.mesh.storage.save_stream = full_disk  # type: ignore[method-assign]
    files = [("a.txt", b"a"), ("b.txt", b"b"), ("c.txt", b"c")]
    job = send(alice, [device_id(alice, "Bravo")], files)
    accept(bob)
    accepted(alice, job)
    for i, (_, data) in enumerate(files):
        upload(alice, job, i, data)

    s = session_of(bob)
    assert (s["state"], s["reason_code"]) == ("partial", "insufficient_storage")
    assert s["reason"].startswith("2 of 3 files arrived.")
    t = target(alice, job)
    assert (t["state"], t["reason_code"]) == ("partial", "insufficient_storage")
    item = history_item(alice, job, "sent")
    assert item["state"] == "partial"
    assert item["recipients"][0]["files_done"] == [0, 1]
    assert history_item(bob, job, "received")["state"] == "partial"


def test_group_with_one_partial_and_one_complete_recipient_is_partial(
    make_node: NodeFactory,
) -> None:
    a, b, c = make_node("Alpha"), make_node("Bravo"), make_node("Charlie")
    connect(a, b)
    connect(a, c)
    alice, bob, carol = owner(a), owner(b), owner(c)
    original = c.mesh.storage.save_stream

    def fail_second(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "two.txt":
            raise InsufficientStorage("Disk full.")
        return original(name, *args, **kwargs)

    c.mesh.storage.save_stream = fail_second  # type: ignore[method-assign]
    job = send(
        alice, [device_id(alice, "Bravo"), device_id(alice, "Charlie")],
        [("one.txt", b"1"), ("two.txt", b"2")],
    )  # fmt: skip
    accept(bob)
    accept(carol)
    wait_for(lambda: {t["state"] for t in _targets(alice, job)} == {"accepted"})
    upload(alice, job, 0, b"1")
    upload(alice, job, 1, b"2")

    states = {t["name"]: t["state"] for t in _targets(alice, job)}
    assert states == {"Bravo": "done", "Charlie": "partial"}
    item = history_item(alice, job, "sent")
    assert item["state"] == "partial"
    assert {r["name"]: r["state"] for r in item["recipients"]} == {
        "Bravo": "completed",
        "Charlie": "partial",
    }


def _targets(client: Client, job_id: str) -> list[Json]:
    for job in client.state()["outgoing"]:
        if job["id"] == job_id:
            return job["targets"]  # type: ignore[no-any-return]
    return []


# ------------------------------------------------------------ disconnects


def test_receiver_disconnect_is_recorded_with_its_reason(make_node: NodeFactory) -> None:
    _a, b, alice, bob = pair_up(make_node)
    job = send(alice, [device_id(alice, "Bravo")], [("f.txt", b"hello")])
    accept(bob)
    accepted(alice, job)
    b.stop()

    status, result = upload(alice, job, 0, b"hello")
    assert status == 200
    assert not any(t["ok"] for t in result["targets"].values())
    t = target(alice, job)
    assert (t["state"], t["reason_code"]) == ("failed", "receiver_disconnected")
    assert history_item(alice, job, "sent")["reason_code"] == "receiver_disconnected"


def test_sender_disconnect_mid_file(make_node: NodeFactory) -> None:
    a, b, alice, bob = pair_up(make_node)
    payload = b"x" * 2_000_000
    job = send(alice, [device_id(alice, "Bravo")], [("big.bin", payload)])
    accept(bob)
    accepted(alice, job)

    # The sending page goes away halfway through the upload.
    conn = http.client.HTTPConnection("127.0.0.1", a.port, timeout=10)
    conn.putrequest("PUT", f"/api/send/{job}/files/0")
    conn.putheader("Content-Length", str(len(payload)))
    conn.putheader("Content-Type", "application/octet-stream")
    conn.putheader("Origin", alice.base)
    conn.endheaders()
    conn.send(payload[: len(payload) // 2])
    conn.close()

    wait_for(lambda: session_of(bob)["state"] == "failed", timeout=20)
    assert session_of(bob)["reason_code"] == "sender_disconnected"
    a.mesh._check_deadlines(later(STALL_TIMEOUT + 1))
    wait_for(lambda: target(alice, job)["state"] == "failed")
    assert target(alice, job)["reason_code"] == "sender_disconnected"
    assert not (Path(b.config.storage_dir) / "big.bin").exists()
    assert not list((Path(b.config.storage_dir) / ".open-transfer" / "incoming").iterdir())


# ---------------------------------------------------------- cancellation


def test_sender_cancels_while_offering(make_node: NodeFactory) -> None:
    _a, _b, alice, bob = pair_up(make_node)
    job = send(alice, [device_id(alice, "Bravo")], [("f.txt", b"hi")])
    incoming(bob)
    assert alice("DELETE", f"/api/send/{job}")[0] == 200
    wait_for(lambda: session_of(bob)["state"] == "canceled")
    assert session_of(bob)["reason_code"] == "sender_cancelled"
    assert target(alice, job)["reason_code"] == "sender_cancelled"


def test_receiver_declines_with_a_reason(make_node: NodeFactory) -> None:
    _a, _b, alice, bob = pair_up(make_node)
    job = send(alice, [device_id(alice, "Bravo")], [("f.txt", b"hi")])
    bob("POST", f"/api/incoming/{incoming(bob)['id']}/decline")
    wait_for(lambda: target(alice, job)["state"] == "declined")
    assert target(alice, job)["reason_code"] == "receiver_declined"


def test_accepting_without_space_declines_with_the_reason(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before: a 507 on Accept left the offer pending until it expired."""
    _a, b, alice, bob = pair_up(make_node)
    job = send(alice, [device_id(alice, "Bravo")], [("f.txt", b"hi")])
    offer = incoming(bob)
    monkeypatch.setattr(b.mesh, "_space_problem", lambda session: "insufficient_storage")
    status, data = bob("POST", f"/api/incoming/{offer['id']}/accept")
    assert status == 507
    assert data["error"]["code"] == "insufficient_storage"
    assert session_of(bob)["state"] == "declined"
    wait_for(lambda: target(alice, job)["state"] == "declined")
    assert target(alice, job)["reason_code"] == "insufficient_storage"


def test_receiver_cancels_after_accepting(make_node: NodeFactory) -> None:
    _a, _b, alice, bob = pair_up(make_node)
    job = send(alice, [device_id(alice, "Bravo")], [("f.txt", b"hi")])
    offer = accept(bob)
    accepted(alice, job)
    assert bob("DELETE", f"/api/incoming/{offer['id']}")[0] == 200

    status, _result = upload(alice, job, 0, b"hi")
    assert status == 200
    t = target(alice, job)
    assert (t["state"], t["reason_code"]) == ("canceled", "receiver_cancelled")


def test_sender_cancels_between_files(make_node: NodeFactory) -> None:
    _a, b, alice, bob = pair_up(make_node)
    job = send(alice, [device_id(alice, "Bravo")], [("one.txt", b"1"), ("two.txt", b"2")])
    accept(bob)
    accepted(alice, job)
    upload(alice, job, 0, b"1")
    assert alice("DELETE", f"/api/send/{job}")[0] == 200
    wait_for(lambda: session_of(bob)["state"] == "canceled")
    assert session_of(bob)["reason_code"] == "sender_cancelled"
    wait_for(lambda: target(alice, job)["state"] == "canceled")
    assert (Path(b.config.storage_dir) / "one.txt").exists()


def test_cancel_racing_completion_keeps_the_delivery(make_node: NodeFactory) -> None:
    """The receiver finished just before the cancel arrived: it was delivered."""
    a, _b, alice, bob = pair_up(make_node)
    job = send(alice, [device_id(alice, "Bravo")], [("f.txt", b"hi")])
    accept(bob)
    accepted(alice, job)
    upload(alice, job, 0, b"hi")
    assert session_of(bob)["state"] == "done"
    # Rewind the sender to "still sending", as if its result hadn't arrived yet.
    delivery = next(iter(a.mesh._jobs[job].deliveries.values()))
    delivery.state, delivery.files_done = "sending", set()
    a.mesh._jobs[job].finished = 0.0

    assert alice("DELETE", f"/api/send/{job}")[0] == 200
    wait_for(lambda: target(alice, job)["state"] == "done")
    assert session_of(bob)["state"] == "done"


def test_group_cancel_reaches_everyone(make_node: NodeFactory) -> None:
    a, b, c = make_node("Alpha"), make_node("Bravo"), make_node("Charlie")
    connect(a, b)
    connect(a, c)
    alice = owner(a)
    job = send(alice, [device_id(alice, "Bravo"), device_id(alice, "Charlie")], [("f", b"x")])
    incoming(owner(b))
    incoming(owner(c))
    alice("DELETE", f"/api/send/{job}")
    for node in (b, c):
        wait_for(lambda node=node: session_of(owner(node))["reason_code"] == "sender_cancelled")
    assert {t["state"] for t in _targets(alice, job)} == {"canceled"}


def test_closing_the_sending_app_fails_the_transfer_on_both_sides(
    make_node: NodeFactory,
) -> None:
    a, _b, alice, bob = pair_up(make_node)
    job = send(alice, [device_id(alice, "Bravo")], [("f.txt", b"hi")])
    incoming(bob)
    a.stop()
    wait_for(lambda: session_of(bob)["state"] == "failed")
    assert session_of(bob)["reason_code"] == "sender_disconnected"
    alice = owner(make_node("Alpha"))
    item = history_item(alice, job, "sent")
    assert (item["state"], item["reason_code"]) == ("failed", "app_closed")


# --------------------------------------------- late and duplicate messages


def test_late_and_duplicate_uploads_are_refused(make_node: NodeFactory) -> None:
    _a, b, alice, bob = pair_up(make_node)
    job = send(alice, [device_id(alice, "Bravo")], [("f.txt", b"hi")])
    offer = accept(bob)
    accepted(alice, job)
    upload(alice, job, 0, b"hi")
    session = b.mesh._incoming[offer["id"]]

    put = http.client.HTTPConnection("127.0.0.1", b.port, timeout=5)
    put.request(
        "PUT", f"{mesh_module.P2P}/offers/{session.id}/files/0", body=b"hi",
        headers={"X-OT-Secret": session.secret, "Content-Length": "2"},
    )  # fmt: skip
    response = put.getresponse()
    assert response.status == 410  # the transfer has ended
    response.read()
    assert session_of(bob)["state"] == "done"


def test_a_cancel_after_completion_changes_nothing(make_node: NodeFactory) -> None:
    _a, b, alice, bob = pair_up(make_node)
    job = send(alice, [device_id(alice, "Bravo")], [("f.txt", b"hi")])
    offer = accept(bob)
    accepted(alice, job)
    upload(alice, job, 0, b"hi")
    session = b.mesh._incoming[offer["id"]]
    b.mesh.cancel_incoming(session.id, None, secret=session.secret)
    assert session.state == "done"
    assert history_item(bob, job, "received")["state"] == "completed"


def test_status_is_still_answered_after_the_live_row_is_gone(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mesh_module, "FINISHED_KEEP", 0.2)
    _a, b, alice, bob = pair_up(make_node)
    job = send(alice, [device_id(alice, "Bravo")], [("f.txt", b"hi")])
    offer = accept(bob)
    accepted(alice, job)
    upload(alice, job, 0, b"hi")
    secret = b.mesh._incoming[offer["id"]].secret
    wait_for(lambda: offer["id"] not in b.mesh._incoming, timeout=10)
    assert b.mesh.offer_status(offer["id"], secret)["state"] == "done"
