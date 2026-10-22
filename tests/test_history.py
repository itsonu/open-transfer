"""Transfer history and activity: what each app records, searching it, and
connection quality (round-trip time, the path a transfer takes)."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from open_transfer.history import History
from tests.conftest import NodeFactory
from tests.test_clipboard import allow, pair, push_text
from tests.test_mesh import (
    Client,
    connect,
    device_id,
    incoming,
    owner,
    send,
    target_states,
    upload,
    wait_for,
)

BRAVO = {"id": "d-" + "b" * 24, "name": "Bravo"}
CHARLIE = {"id": "d-" + "c" * 24, "name": "Charlie"}


def entry(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "kind": "transfer",
        "direction": "sent",
        "title": "photo.jpg",
        "files": 1,
        "size": 1000,
        "status": "done",
        "devices": [{**BRAVO, "status": "done"}],
    }
    return {**base, **overrides}


# --------------------------------------------------------------------- store


def test_newest_first_and_filters(tmp_path: Path) -> None:
    history = History(tmp_path)
    history.add(entry(title="photo.jpg"))
    history.add(
        entry(title="notes.txt", direction="received", devices=[{**CHARLIE, "status": "done"}])
    )
    history.add(entry(title="big.zip", status="failed"))
    history.add(entry(kind="clipboard", title="Text", size=12))
    assert [e["title"] for e in history.query()] == ["Text", "big.zip", "notes.txt", "photo.jpg"]
    assert [e["title"] for e in history.query(q="NOTES")] == ["notes.txt"]
    assert [e["title"] for e in history.query(q="charlie")] == ["notes.txt"]  # device names match
    assert [e["title"] for e in history.query(device=CHARLIE["id"])] == ["notes.txt"]
    assert [e["title"] for e in history.query(direction="received")] == ["notes.txt"]
    assert [e["title"] for e in history.query(status="failed")] == ["big.zip"]
    assert [e["title"] for e in history.query(kind="clipboard")] == ["Text"]
    assert len(history.query(limit=2)) == 2
    assert history.latest() == history.query()[0]["id"]


def test_since(tmp_path: Path) -> None:
    history = History(tmp_path)
    old = history.add(entry(title="old"))
    old["at"] = time.time() - 3600
    history._entries[0]["at"] = old["at"]
    history.add(entry(title="new"))
    assert [e["title"] for e in history.query(since=time.time() - 60)] == ["new"]


def test_survives_a_restart_is_capped_and_private(tmp_path: Path) -> None:
    history = History(tmp_path, limit=5)
    for i in range(8):
        history.add(entry(title=f"file {i}"))
    again = History(tmp_path, limit=5)
    assert [e["title"] for e in again.query()] == [f"file {i}" for i in range(7, 2, -1)]
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / "history.jsonl").stat().st_mode) == 0o600
    again.clear()
    assert History(tmp_path).query() == []


def test_a_damaged_line_is_skipped(tmp_path: Path) -> None:
    history = History(tmp_path)
    history.add(entry(title="kept"))
    with (tmp_path / "history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    history.add(entry(title="also kept"))
    assert [e["title"] for e in History(tmp_path).query()] == ["also kept", "kept"]


# ------------------------------------------------------------ recorded by apps


def history_of(client: Client, query: str = "") -> list[dict]:  # type: ignore[type-arg]
    status, data = client("GET", f"/api/history{query}")
    assert status == 200, data
    return data["entries"]  # type: ignore[no-any-return]


def test_both_sides_record_a_transfer(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice, bob = owner(a), owner(b)
    latest = alice.state()["history"]["latest"]
    job = send(alice, [device_id(alice, "Bravo")], [("trip.jpg", b"x" * 5000)])
    session = incoming(bob)
    bob("POST", f"/api/incoming/{session['id']}/accept")
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    assert upload(alice, job, 0, b"x" * 5000)[0] == 200
    sent = wait_for(lambda: history_of(alice))[0]
    assert sent["direction"] == "sent"
    assert sent["title"] == "trip.jpg"
    assert sent["size"] == 5000
    assert sent["status"] == "done"
    assert [(d["name"], d["status"]) for d in sent["devices"]] == [("Bravo", "done")]
    assert sent["speed"] > 0
    assert sent["ref"] == job
    assert alice.state()["history"]["latest"] != latest
    received = wait_for(lambda: history_of(bob))[0]
    assert received["direction"] == "received"
    assert received["title"] == "trip.jpg"
    assert [d["name"] for d in received["devices"]] == ["Alpha"]
    assert history_of(bob, "?direction=sent") == []


def test_declined_transfers_are_recorded_too(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice, bob = owner(a), owner(b)
    send(alice, [device_id(alice, "Bravo")], [("nope.txt", b"x")])
    session = incoming(bob)
    bob("POST", f"/api/incoming/{session['id']}/decline")
    assert wait_for(lambda: history_of(alice, "?status=declined"))[0]["title"] == "nope.txt"
    assert wait_for(lambda: history_of(bob, "?status=declined"))[0]["direction"] == "received"


def test_clipboard_activity_never_records_what_was_copied(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    alice, bob = owner(a), owner(b)
    pair(alice, bob, b.port)
    allow(alice, device_id(alice, "Bravo"))
    allow(bob, device_id(bob, "Alpha"))
    assert push_text(alice, "Meet at the north gate")[0] == 200
    sent = wait_for(lambda: history_of(alice, "?kind=clipboard"))[0]
    assert sent["title"] == "Text"
    assert [d["name"] for d in sent["devices"]] == ["Bravo"]
    received = wait_for(lambda: history_of(bob, "?kind=clipboard"))[0]
    assert received["direction"] == "received"
    for node in (a, b):
        assert b"north gate" not in (node.config.state_path / "history.jsonl").read_bytes()


def test_history_is_for_the_owner_only(make_node: NodeFactory) -> None:
    a = make_node("Alpha", owner_loopback=False)
    visitor = Client(a.local_url)
    assert visitor("GET", "/api/history")[0] == 403
    assert visitor("DELETE", "/api/history")[0] == 403


def test_clearing_history(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice, bob = owner(a), owner(b)
    send(alice, [device_id(alice, "Bravo")], [("x.txt", b"x")])
    bob("POST", f"/api/incoming/{incoming(bob)['id']}/decline")
    wait_for(lambda: history_of(alice))
    assert alice("DELETE", "/api/history")[0] == 200
    assert history_of(alice) == []


# ------------------------------------------------------------ connection quality


def test_round_trip_time_of_nearby_apps(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice = owner(a)

    def rtt() -> int | None:
        bravo = next(d for d in alice.state()["devices"] if d["name"] == "Bravo")
        return bravo.get("rtt")

    value = wait_for(lambda: rtt() is not None and rtt(), timeout=20) or rtt()
    assert isinstance(value, int)
    assert 0 <= value < 5000


@pytest.mark.parametrize("to_browser", [False, True])
def test_each_target_says_which_way_it_goes(make_node: NodeFactory, to_browser: bool) -> None:
    a, b = make_node("Alpha"), make_node("Bravo", owner_loopback=False)
    connect(a, b)
    alice = owner(a)
    if to_browser:
        phone = Client(b.local_url)  # a browser visiting Bravo
        assert (
            phone("POST", "/api/me", {"name": "Phone", "form": "phone", "platform": "android"})[0]
            == 200
        )
        target = device_id(alice, "Phone")
    else:
        target = device_id(alice, "Bravo")
    job = send(alice, [target], [("x.txt", b"x")])
    view = next(j for j in alice.state()["outgoing"] if j["id"] == job)
    assert view["targets"][0]["path"] == ("via" if to_browser else "app")
