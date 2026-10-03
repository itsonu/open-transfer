"""Group sends: one parent transfer, a result per recipient, and an honest summary."""

from __future__ import annotations

import http.client
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from open_transfer import mesh as mesh_module
from open_transfer.mesh import OFFER_TTL, SENDER_GRACE
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


def group(make_node: NodeFactory, *names: str) -> tuple[Node, Client, dict[str, Node]]:
    sender = make_node("Alpha")
    receivers = {name: make_node(name) for name in names}
    for node in receivers.values():
        connect(sender, node)
    return sender, owner(sender), receivers


def job_view(client: Client, job_id: str) -> Json:
    for job in client.state()["outgoing"]:
        if job["id"] == job_id:
            return job  # type: ignore[no-any-return]
    raise AssertionError("job not found")


def states(client: Client, job_id: str) -> dict[str, str]:
    return {t["name"]: t["state"] for t in job_view(client, job_id)["targets"]}


def answer(node: Node, accept: bool) -> None:
    bob = owner(node)
    offer = incoming(bob)
    bob("POST", f"/api/incoming/{offer['id']}/{'accept' if accept else 'decline'}")


def sent_record(client: Client, job_id: str) -> Json:
    status, data = client("GET", "/api/history?direction=sent")
    assert status == 200
    return next(i for i in data["items"] if i["transfer_id"] == job_id)


def test_one_recipient_finishes_while_another_has_not_answered(make_node: NodeFactory) -> None:
    _a, alice, r = group(make_node, "Bravo", "Charlie")
    ids = [device_id(alice, n) for n in r]
    job = send(alice, ids, [("f.txt", b"hello")])
    answer(r["Bravo"], accept=True)
    wait_for(lambda: states(alice, job)["Bravo"] == "accepted")
    assert upload(alice, job, 0, b"hello")[0] == 200

    view = job_view(alice, job)
    assert states(alice, job) == {"Bravo": "done", "Charlie": "waiting"}
    assert view["state"] == "offered"  # still going: never "completed" while someone is out
    assert (view["summary"]["delivered"], view["summary"]["pending"]) == (1, 1)

    answer(r["Charlie"], accept=True)
    wait_for(lambda: states(alice, job)["Charlie"] == "accepted")
    assert upload(alice, job, 0, b"hello")[0] == 200  # only Charlie still needs it
    view = job_view(alice, job)
    assert view["state"] == "completed"
    assert view["summary"]["delivered"] == 2
    assert (Path(r["Charlie"].config.storage_dir) / "f.txt").read_bytes() == b"hello"
    assert not (Path(r["Bravo"].config.storage_dir) / "f (1).txt").exists()  # not sent twice


def test_decline_and_disconnect_leave_the_others_delivered(make_node: NodeFactory) -> None:
    _a, alice, r = group(make_node, "Bravo", "Charlie", "Delta")
    job = send(alice, [device_id(alice, n) for n in r], [("f.txt", b"hi")])
    answer(r["Bravo"], accept=True)
    answer(r["Charlie"], accept=False)
    answer(r["Delta"], accept=True)
    wait_for(
        lambda: (
            states(alice, job) == {"Bravo": "accepted", "Charlie": "declined", "Delta": "accepted"}
        )
    )
    r["Delta"].stop()  # gone before the bytes come
    assert upload(alice, job, 0, b"hi")[0] == 200

    view = job_view(alice, job)
    assert view["state"] == "partial"
    s = view["summary"]
    assert (s["total"], s["delivered"], s["declined"], s["failed"], s["disconnected"]) == (
        3,
        1,
        1,
        1,
        1,
    )
    by_name = {t["name"]: t for t in view["targets"]}
    assert by_name["Bravo"]["state"] == "done"
    assert by_name["Charlie"]["reason_code"] == "receiver_declined"
    assert by_name["Delta"]["reason_code"] == "receiver_disconnected"
    assert by_name["Delta"]["disconnected"] is True
    assert (Path(r["Bravo"].config.storage_dir) / "f.txt").read_bytes() == b"hi"


def test_send_now_skips_the_unanswered_recipient(make_node: NodeFactory) -> None:
    _a, alice, r = group(make_node, "Bravo", "Charlie")
    job = send(alice, [device_id(alice, n) for n in r], [("f.txt", b"hi")])
    answer(r["Bravo"], accept=True)
    wait_for(lambda: states(alice, job)["Bravo"] == "accepted")
    charlie = device_id(alice, "Charlie")
    assert alice("DELETE", f"/api/send/{job}/targets/{charlie}")[0] == 200  # "Send now"
    assert upload(alice, job, 0, b"hi")[0] == 200

    view = job_view(alice, job)
    assert states(alice, job) == {"Bravo": "done", "Charlie": "canceled"}
    assert (view["state"], view["summary"]["delivered"], view["summary"]["cancelled"]) == (
        "partial",
        1,
        1,
    )
    wait_for(lambda: incoming(owner(r["Charlie"]), "canceled")["reason_code"] == "sender_cancelled")


def test_cancelling_one_recipient_mid_upload_does_not_hold_up_the_others(
    make_node: NodeFactory,
) -> None:
    a, alice, r = group(make_node, "Bravo", "Charlie")
    payload = b"x" * 4_000_000
    job = send(alice, [device_id(alice, n) for n in r], [("big.bin", payload)])
    answer(r["Bravo"], accept=True)
    answer(r["Charlie"], accept=True)
    wait_for(lambda: set(states(alice, job).values()) == {"accepted"})

    conn = http.client.HTTPConnection("127.0.0.1", a.port, timeout=60)
    conn.putrequest("PUT", f"/api/send/{job}/files/0")
    for header, value in (
        ("Content-Length", str(len(payload))),
        ("Content-Type", "application/octet-stream"),
        ("Origin", alice.base),
    ):
        conn.putheader(header, value)
    conn.endheaders()
    conn.send(payload[:1_000_000])
    wait_for(lambda: all(t["sent"] > 0 for t in job_view(alice, job)["targets"]))
    charlie = device_id(alice, "Charlie")
    assert alice("DELETE", f"/api/send/{job}/targets/{charlie}")[0] == 200
    started = time.monotonic()
    conn.send(payload[1_000_000:])
    response = conn.getresponse()
    response.read()
    assert response.status == 200
    assert time.monotonic() - started < 15  # before: waited up to 120 s for the canceled stream

    wait_for(lambda: states(alice, job) == {"Bravo": "done", "Charlie": "canceled"})
    assert (Path(r["Bravo"].config.storage_dir) / "big.bin").read_bytes() == payload
    assert not (Path(r["Charlie"].config.storage_dir) / "big.bin").exists()


def test_cancelling_the_group_reaches_every_active_recipient(make_node: NodeFactory) -> None:
    _a, alice, r = group(make_node, "Bravo", "Charlie", "Delta")
    job = send(alice, [device_id(alice, n) for n in r], [("f.txt", b"hi")])
    answer(r["Bravo"], accept=True)
    answer(r["Charlie"], accept=False)
    wait_for(
        lambda: (
            states(alice, job)["Bravo"] == "accepted"
            and states(alice, job)["Charlie"] == "declined"
        )
    )
    assert alice("DELETE", f"/api/send/{job}")[0] == 200

    wait_for(
        lambda: (
            states(alice, job) == {"Bravo": "canceled", "Charlie": "declined", "Delta": "canceled"}
        )
    )
    for name in ("Bravo", "Delta"):
        wait_for(
            lambda name=name: (
                owner(r[name]).state()["incoming"][-1]["reason_code"] == "sender_cancelled"
            )
        )
    summary = job_view(alice, job)["summary"]
    assert (summary["state"], summary["cancelled"], summary["declined"]) == ("cancelled", 2, 1)


def test_everyone_declining_or_expiring_is_reported_as_such(make_node: NodeFactory) -> None:
    a, alice, r = group(make_node, "Bravo", "Charlie")
    declined = send(alice, [device_id(alice, n) for n in r], [("f.txt", b"hi")])
    answer(r["Bravo"], accept=False)
    answer(r["Charlie"], accept=False)
    wait_for(lambda: job_view(alice, declined)["state"] == "declined")

    expired = send(alice, [device_id(alice, n) for n in r], [("g.txt", b"hi")])
    wait_for(lambda: len(owner(r["Charlie"]).state()["incoming"]) == 2)
    for node in r.values():
        node.mesh._check_deadlines(time.monotonic() + OFFER_TTL + 1)
    a.mesh._check_deadlines(time.monotonic() + OFFER_TTL + SENDER_GRACE + 1)
    wait_for(lambda: job_view(alice, expired)["state"] == "expired")
    assert job_view(alice, expired)["summary"]["expired"] == 2


def test_per_file_results_per_recipient(make_node: NodeFactory) -> None:
    _a, alice, r = group(make_node, "Bravo", "Charlie")
    original = r["Charlie"].mesh.storage.save_stream

    def no_room_for_two(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "two.txt":
            raise InsufficientStorage("Disk full.")
        return original(name, *args, **kwargs)

    r["Charlie"].mesh.storage.save_stream = no_room_for_two  # type: ignore[method-assign]
    job = send(alice, [device_id(alice, n) for n in r], [("one.txt", b"1"), ("two.txt", b"2")])
    answer(r["Bravo"], accept=True)
    answer(r["Charlie"], accept=True)
    wait_for(lambda: set(states(alice, job).values()) == {"accepted"})
    upload(alice, job, 0, b"1")
    upload(alice, job, 1, b"2")

    by_name = {t["name"]: t for t in job_view(alice, job)["targets"]}
    assert (by_name["Bravo"]["files_done"], by_name["Bravo"]["files_failed"]) == ([0, 1], {})
    assert by_name["Charlie"]["files_done"] == [0]
    assert by_name["Charlie"]["files_failed"] == {"1": "insufficient_storage"}
    item = sent_record(alice, job)
    rec = {x["name"]: x for x in item["recipients"]}
    assert rec["Charlie"]["files_failed"] == {"1": "insufficient_storage"}
    assert rec["Charlie"]["state"] == "partial"
    assert item["summary"]["partial"] == 1


def test_group_result_survives_the_live_row_and_a_restart(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mesh_module, "FINISHED_KEEP", 0.3)
    a, alice, r = group(make_node, "Bravo", "Charlie", "Delta")
    job = send(alice, [device_id(alice, n) for n in r], [("f.txt", b"hi")])
    answer(r["Bravo"], accept=True)
    answer(r["Charlie"], accept=True)
    answer(r["Delta"], accept=False)
    wait_for(
        lambda: (
            states(alice, job)["Charlie"] == "accepted"
            and states(alice, job)["Bravo"] == "accepted"
        )
    )
    upload(alice, job, 0, b"hi")
    wait_for(lambda: not alice.state()["outgoing"], timeout=15)

    a.stop()
    alice = owner(make_node("Alpha"))
    item = sent_record(alice, job)
    assert item["state"] == "partial"
    assert item["summary"]["total"] == 3
    assert item["summary"]["delivered"] == 2
    rec = {x["name"]: x for x in item["recipients"]}
    assert rec["Delta"]["reason_code"] == "receiver_declined"
    assert rec["Bravo"]["finished_at"] >= rec["Bravo"]["started_at"] > 0
    assert rec["Delta"]["started_at"] is None
    assert rec["Delta"]["finished_at"] > 0


def test_recipient_states_never_overwrite_each_other(make_node: NodeFactory) -> None:
    """Concurrent answers from several receivers each land on their own recipient."""
    _a, alice, r = group(make_node, "Bravo", "Charlie", "Delta")
    job = send(alice, [device_id(alice, n) for n in r], [("f.txt", b"hi")])
    threads = [
        threading.Thread(target=answer, args=(node, name != "Charlie")) for name, node in r.items()
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wait_for(
        lambda: (
            states(alice, job) == {"Bravo": "accepted", "Charlie": "declined", "Delta": "accepted"}
        )
    )
