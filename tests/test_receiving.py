"""Receiving safely: files that are already here, and checking every file
arrived intact (SHA-256 on both ends of an app-to-app transfer)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from open_transfer import mesh as mesh_module
from tests.conftest import NodeFactory
from tests.test_mesh import (
    Client,
    connect,
    device_id,
    incoming,
    owner,
    send,
    send_in_chunks,
    target_states,
    upload,
    wait_for,
)


def received_files(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_file())


def offer_and_accept(
    alice: Client, bob: Client, files: list[tuple[str, bytes]], **decision: object
) -> tuple[str, dict]:  # type: ignore[type-arg]
    job = send(alice, [device_id(alice, "Bravo")], files)
    session = incoming(bob)
    status, data = bob("POST", f"/api/incoming/{session['id']}/accept", decision or None)
    assert status == 200, data
    return job, session


# --------------------------------------------------------------- duplicates


def test_files_already_here_are_flagged(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    root = Path(b.config.storage_dir)
    (root / "report.pdf").write_bytes(b"o" * 10)
    (root / "photo.jpg").write_bytes(b"o" * 99)  # same name, different size: not a duplicate
    alice, bob = owner(a), owner(b)
    send(
        alice,
        [device_id(alice, "Bravo")],
        [("report.pdf", b"n" * 10), ("photo.jpg", b"n" * 10), ("new.txt", b"x")],
    )
    session = incoming(bob)
    assert [f["duplicate"] for f in session["files"]] == [True, False, False]


@pytest.mark.parametrize(
    ("policy", "expected_files", "report"),
    [
        (None, ["new.txt", "report (1).pdf", "report.pdf"], b"o" * 10),  # keep both (default)
        ("rename", ["new.txt", "report (1).pdf", "report.pdf"], b"o" * 10),
        ("skip", ["new.txt", "report.pdf"], b"o" * 10),
        ("replace", ["new.txt", "report.pdf"], b"n" * 10),
    ],
)
def test_what_to_do_with_files_already_here(
    make_node: NodeFactory, policy: str | None, expected_files: list[str], report: bytes
) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    root = Path(b.config.storage_dir)
    (root / "report.pdf").write_bytes(b"o" * 10)
    alice, bob = owner(a), owner(b)
    decision = {"duplicates": policy} if policy else {}
    job, _ = offer_and_accept(
        alice, bob, [("report.pdf", b"n" * 10), ("new.txt", b"x")], **decision
    )
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    assert upload(alice, job, 0, b"n" * 10)[0] == 200
    assert upload(alice, job, 1, b"x")[0] == 200
    wait_for(lambda: target_states(alice, job) == {"Bravo": "done"})
    assert received_files(root) == expected_files
    assert (root / "report.pdf").read_bytes() == report


def test_skipping_works_for_big_files_too(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    big = b"z" * (5 * 1024 * 1024)
    root = Path(b.config.storage_dir)
    (root / "movie.mp4").write_bytes(big)
    alice, bob = owner(a), owner(b)
    job, _ = offer_and_accept(alice, bob, [("movie.mp4", big)], duplicates="skip")
    wait_for(
        lambda: (
            target_states(alice, job) == {"Bravo": "done"}
            or target_states(alice, job) == {"Bravo": "accepted"}
        )
    )
    send_in_chunks(alice, job, big, "u-skipbig")
    wait_for(lambda: target_states(alice, job) == {"Bravo": "done"})
    assert received_files(root) == ["movie.mp4"]


def test_unknown_duplicate_choice_is_refused(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice, bob = owner(a), owner(b)
    send(alice, [device_id(alice, "Bravo")], [("a.txt", b"x")])
    session = incoming(bob)
    status, data = bob("POST", f"/api/incoming/{session['id']}/accept", {"duplicates": "explode"})
    assert status == 400, data
    assert bob.state()["incoming"][0]["state"] == "pending"


# ---------------------------------------------------------------- integrity


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("chunked", [False, True])
def test_each_file_is_checked_end_to_end(make_node: NodeFactory, chunked: bool) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice, bob = owner(a), owner(b)
    payload = (b"0123456789abcdef" * 400_000) if chunked else b"hello, intact world"
    job, session = offer_and_accept(alice, bob, [("data.bin", payload)])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    if chunked:
        send_in_chunks(alice, job, payload, "u-integrity")
    else:
        assert upload(alice, job, 0, payload)[0] == 200
    wait_for(lambda: target_states(alice, job) == {"Bravo": "done"})
    view = next(j for j in alice.state()["outgoing"] if j["id"] == job)
    assert view["targets"][0]["verified"] is True
    files = next(s for s in bob.state()["incoming"] if s["id"] == session["id"])["files"]
    assert files[0]["sha256"] == sha(payload)
    assert files[0]["verified"] is True


def test_a_damaged_file_is_removed_and_reported(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice, bob = owner(a), owner(b)
    # The sender's checksum disagrees with what arrived, as if bytes changed on the way.
    monkeypatch.setattr(mesh_module, "_claimed_digest", lambda hasher: "0" * 64)
    job, session = offer_and_accept(alice, bob, [("contract.pdf", b"%PDF important")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    upload(alice, job, 0, b"%PDF important")
    wait_for(lambda: target_states(alice, job) == {"Bravo": "failed"})
    target = next(j for j in alice.state()["outgoing"] if j["id"] == job)["targets"][0]
    assert "damaged" in target["reason"]
    assert received_files(Path(b.config.storage_dir)) == []
    received = next(s for s in bob.state()["incoming"] if s["id"] == session["id"])
    assert received["state"] == "failed"
    assert "damaged" in received["files"][0]["error"]


def test_a_stale_view_of_kept_files_is_rechecked(make_node: NodeFactory) -> None:
    # The sender's poll can lag behind the receiver's answer: it must ask again
    # before saying nobody accepted.
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    big = b"k" * (5 * 1024 * 1024)
    (Path(b.config.storage_dir) / "movie.mp4").write_bytes(big)
    alice, bob = owner(a), owner(b)
    job, _ = offer_and_accept(
        alice, bob, [("movie.mp4", big), ("new.txt", b"x")], duplicates="skip"
    )
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    for delivery in a.mesh._jobs[job].deliveries.values():
        delivery.skip.clear()
    send_in_chunks(alice, job, big, "u-stale")
    assert upload(alice, job, 1, b"x")[0] == 200
    wait_for(lambda: target_states(alice, job) == {"Bravo": "done"})
    assert received_files(Path(b.config.storage_dir)) == ["movie.mp4", "new.txt"]
