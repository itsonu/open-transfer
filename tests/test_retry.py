"""Retry: only the devices that didn't get it are asked again, and files that
already arrived aren't sent (or saved) a second time."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import NodeFactory
from tests.test_encrypted_transfers import paired
from tests.test_mesh import connect, incoming, owner, send, target_states, upload, wait_for


def retry(node, job: str, to: list[str]) -> str:  # type: ignore[no-untyped-def]
    status, data = owner(node)("POST", f"/api/send/{job}/retry", {"to": to})
    assert status == 201, data
    return str(data["job"]["id"])


def test_a_retried_multi_file_send_skips_what_arrived(make_node: NodeFactory) -> None:
    a, b = paired(make_node)  # paired: accepted without asking
    alice = owner(a)
    files = [("one.txt", b"first"), ("two.txt", b"second")]
    job = send(alice, [b.mesh.id], files)
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    assert upload(alice, job, 0, b"first")[0] == 200
    wait_for(lambda: (Path(b.config.storage_dir) / "one.txt").exists())
    alice("DELETE", f"/api/send/{job}/targets/{b.mesh.id}")  # the window closed partway

    again = retry(a, job, [b.mesh.id])
    wait_for(lambda: target_states(alice, again) == {"Bravo": "accepted"})
    for index, (_, data) in enumerate(files):  # the page sends everything it has
        upload(alice, again, index, data)
    wait_for(lambda: target_states(alice, again) == {"Bravo": "done"})
    got = sorted(p.name for p in Path(b.config.storage_dir).iterdir() if not p.name.startswith("."))
    assert got == ["one.txt", "two.txt"]  # not "one (1).txt"


def test_an_unpaired_receiver_is_offered_skip_for_a_retry(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    (Path(b.config.storage_dir) / "one.txt").write_bytes(b"first")
    alice = owner(a)
    job = send(alice, [b.mesh.id], [("one.txt", b"first")])
    first = incoming(owner(b))
    assert first["retry"] is False
    owner(b)("POST", f"/api/incoming/{first['id']}/decline")
    wait_for(lambda: target_states(alice, job) == {"Bravo": "declined"})
    retry(a, job, [b.mesh.id])
    again = wait_for(
        lambda: next((i for i in owner(b).state()["incoming"] if i["state"] == "pending"), None)
    )
    assert again["retry"] is True  # the prompt picks "Skip" for files already here


def test_a_group_retry_asks_only_who_didnt_get_it(make_node: NodeFactory) -> None:
    a, b, c = make_node("Alpha"), make_node("Bravo"), make_node("Charlie")
    connect(a, b)
    connect(a, c)
    alice = owner(a)
    job = send(alice, [b.mesh.id, c.mesh.id], [("plan.txt", b"plan")])
    owner(b)("POST", f"/api/incoming/{incoming(owner(b))['id']}/accept")
    owner(c)("POST", f"/api/incoming/{incoming(owner(c))['id']}/decline")
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted", "Charlie": "declined"})
    upload(alice, job, 0, b"plan")
    wait_for(lambda: target_states(alice, job) == {"Bravo": "done", "Charlie": "declined"})

    again = retry(a, job, [b.mesh.id, c.mesh.id])  # "Retry" on the whole group
    assert set(target_states(alice, again)) == {"Charlie"}
    wait_for(lambda: len(owner(c).state()["incoming"]) == 2)
    assert len(owner(b).state()["incoming"]) == 1  # Bravo isn't asked again
