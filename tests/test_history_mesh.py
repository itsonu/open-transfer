"""Transfer history end to end: real nodes record what they send and receive."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from open_transfer import mesh as mesh_module
from open_transfer.history import FileRecord, History, RecipientRecord, TransferRecord
from tests import test_mesh
from tests.test_mesh import (
    Client,
    NodeFactory,
    connect,
    device_id,
    incoming,
    owner,
    send,
    target_states,
    upload,
    visitor,
    wait_for,
)

Json = dict[str, Any]
make_node = test_mesh.make_node  # the shared pytest fixture


def history(client: Client, query: str = "") -> list[Json]:
    status, data = client("GET", f"/api/history{query}")
    assert status == 200, data
    return data["items"]  # type: ignore[no-any-return]


def record(client: Client, transfer_id: str, state: str, direction: str) -> Json:
    """Wait for the history record of ``transfer_id`` to reach ``state``."""

    def find() -> Json | None:
        for item in history(client):
            if (
                item["transfer_id"] == transfer_id
                and item["direction"] == direction
                and item["state"] == state
            ):
                return item
        return None

    return wait_for(find)  # type: ignore[no-any-return]


def test_a_real_transfer_is_recorded_on_both_sides(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice, bob = owner(a), owner(b)
    payload = b"x" * 200_000
    job = send(alice, [device_id(alice, "Bravo")], [("holiday.jpg", payload)])

    offered = record(bob, job, "offered", "received")
    assert offered["sender"]["name"] == "Alpha"
    assert record(alice, job, "offered", "sent")["recipients"][0]["name"] == "Bravo"

    assert bob("POST", f"/api/incoming/{incoming(bob)['id']}/accept")[0] == 200
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    assert upload(alice, job, 0, payload)[0] == 200

    sent = record(alice, job, "completed", "sent")
    assert sent["total_bytes"] == len(payload)
    assert sent["files"][0]["name"] == "holiday.jpg"
    assert sent["started_at"] is not None
    assert sent["finished_at"] >= sent["created_at"]
    [recipient] = sent["recipients"]
    assert recipient["state"] == "completed"
    assert recipient["bytes_done"] == len(payload)
    assert recipient["files_done"] == [0]

    received = record(bob, job, "completed", "received")
    assert received["sender"]["name"] == "Alpha"
    assert received["peer_node"] == alice.state()["host"]["id"]
    assert received["files"][0]["saved_name"] == "holiday.jpg"
    assert received["files"][0]["exists"] is True
    assert received["recipients"][0]["state"] == "completed"


def test_group_send_is_one_record_with_a_result_per_recipient(make_node: NodeFactory) -> None:
    a, b, c = make_node("Alpha"), make_node("Bravo"), make_node("Charlie")
    connect(a, b)
    connect(a, c)
    alice = owner(a)
    job = send(alice, [device_id(alice, "Bravo"), device_id(alice, "Charlie")], [("f.txt", b"hi")])
    owner(b)("POST", f"/api/incoming/{incoming(owner(b))['id']}/accept")
    owner(c)("POST", f"/api/incoming/{incoming(owner(c))['id']}/decline")
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted", "Charlie": "declined"})
    assert upload(alice, job, 0, b"hi")[0] == 200

    sent = record(alice, job, "partial", "sent")
    assert {r["name"]: r["state"] for r in sent["recipients"]} == {
        "Bravo": "completed",
        "Charlie": "declined",
    }
    assert [i["transfer_id"] for i in history(alice) if i["direction"] == "sent"] == [job]
    assert record(owner(c), job, "declined", "received")["sender"]["name"] == "Alpha"


def test_finished_records_outlive_the_live_transfer(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mesh_module, "FINISHED_KEEP", 0.5)
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice, bob = owner(a), owner(b)
    job = send(alice, [device_id(alice, "Bravo")], [("f.txt", b"hi")])
    bob("POST", f"/api/incoming/{incoming(bob)['id']}/decline")
    record(alice, job, "declined", "sent")

    wait_for(lambda: not alice.state()["outgoing"] and not bob.state()["incoming"], timeout=15)
    assert record(alice, job, "declined", "sent")["recipients"][0]["state"] == "declined"
    assert record(bob, job, "declined", "received")


def test_offline_target_failure_is_recorded(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice = owner(a)
    bravo = device_id(alice, "Bravo")
    b.stop()
    job = send(alice, [bravo], [("f.txt", b"hi")])
    failed = record(alice, job, "failed", "sent")
    assert failed["recipients"][0]["reason"]
    assert [i["transfer_id"] for i in history(alice, "?failed=1")] == [job]


def test_deleting_the_file_keeps_the_record(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice, bob = owner(a), owner(b)
    job = send(alice, [device_id(alice, "Bravo")], [("notes.txt", b"hello")])
    bob("POST", f"/api/incoming/{incoming(bob)['id']}/accept")
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    upload(alice, job, 0, b"hello")
    record(bob, job, "completed", "received")

    assert bob("DELETE", "/api/files/notes.txt")[0] == 200
    item = record(bob, job, "completed", "received")
    assert item["files"][0]["exists"] is False
    assert item["files"][0]["saved_name"] == "notes.txt"


def test_removing_a_record_keeps_the_file(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice, bob = owner(a), owner(b)
    job = send(alice, [device_id(alice, "Bravo")], [("keep.txt", b"hello")])
    bob("POST", f"/api/incoming/{incoming(bob)['id']}/accept")
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    upload(alice, job, 0, b"hello")
    item = record(bob, job, "completed", "received")

    assert bob("DELETE", f"/api/history/{item['row_id']}")[0] == 200
    assert history(bob) == []
    assert (Path(b.config.storage_dir) / "keep.txt").read_bytes() == b"hello"

    record(alice, job, "completed", "sent")
    assert alice("POST", "/api/history/clear", {"completed_only": True}) == (200, {"removed": 1})
    assert history(alice) == []


def test_restart_turns_unfinished_transfers_into_failures(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice, bob = owner(a), owner(b)
    job = send(alice, [device_id(alice, "Bravo")], [("f.txt", b"hi")])
    record(bob, job, "offered", "received")

    # A crash: the server goes away without the app's own shutdown.
    b.mesh._stop.set()
    b._server.stop()
    bob = owner(make_node("Bravo"))
    item = record(bob, job, "failed", "received")
    assert item["reason_code"] == "app_restart"
    assert item["recipients"][0]["reason_code"] == "app_restart"


def test_closing_the_app_fails_active_transfers_as_app_closed(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice, bob = owner(a), owner(b)
    job = send(alice, [device_id(alice, "Bravo")], [("f.txt", b"hi")])
    record(bob, job, "offered", "received")

    b.stop()  # closed on purpose while the offer is unanswered
    bob = owner(make_node("Bravo"))
    item = record(bob, job, "failed", "received")
    assert item["reason_code"] == "app_closed"


def test_history_is_owner_only(make_node: NodeFactory) -> None:
    phone = visitor(make_node("Alpha"), "Phone")
    assert phone("GET", "/api/history")[0] == 403
    assert phone("DELETE", "/api/history/1")[0] == 403
    assert phone("POST", "/api/history/clear", {})[0] == 403


def test_retention_is_applied_on_start(tmp_path: Path, make_node: NodeFactory) -> None:
    state = tmp_path / "Bravo" / ".open-transfer"
    old = History(state / "history.db")
    old.save(
        TransferRecord(
            transfer_id="ancient",
            direction="received",
            files=[FileRecord(name="old.txt", size=1)],
            recipients=[RecipientRecord(device_id="d-00000000", device={}, state="completed")],
            sender={"name": "Someone"},
            created_at=time.time() - 400 * 24 * 3600,
        )
    )
    old.close()
    assert history(owner(make_node("Bravo"))) == []
