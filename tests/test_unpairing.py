"""Unpairing: the other device is told, old keys stop working, history stays.

When one side unpairs, it tells the other in a request signed with the pair key
(the last time that key is used), and the other forgets the pairing too. If
the other was away, its next send gets ``peer_unpaired`` and a "Pair again".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from open_transfer.node import Node
from tests.conftest import NodeFactory, Tap
from tests.test_encrypted_transfers import paired, target
from tests.test_mesh import Client, owner, send, target_states, upload, wait_for
from tests.test_moves import pair


def test_the_other_device_is_told_and_forgets_the_pairing(make_node: NodeFactory) -> None:
    a, b = paired(make_node)
    assert a.mesh.unpair(b.mesh.id)
    wait_for(lambda: b.mesh.trust.get(a.mesh.id) is None)
    assert owner(b).state()["keys"]["unpaired_by"] == ["Alpha"]  # said, with Pair again


def test_a_forged_unpair_notice_is_ignored(make_node: NodeFactory) -> None:
    a, b = paired(make_node)
    status, _ = Client(b.local_url)(
        "POST",
        "/api/p2p/v1/unpaired",
        {"id": a.mesh.id},
        headers={"X-OT-From": a.mesh.id, "X-OT-Auth": "1:00"},
    )
    assert status == 403
    assert b.mesh.trust.get(a.mesh.id) is not None


def missed_notice(node: Node, monkeypatch: pytest.MonkeyPatch) -> None:
    real = node.mesh._http

    def drop(peer: Any, method: str, path: str, **kw: Any) -> tuple[int, dict[str, Any]]:
        if path.endswith("/unpaired"):
            raise OSError("the other device was away")
        return real(peer, method, path, **kw)

    monkeypatch.setattr(node.mesh, "_http", drop)


def test_a_device_that_missed_it_gets_peer_unpaired_and_pair_again(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch, tap: Tap
) -> None:
    a, b = paired(make_node)
    missed_notice(a, monkeypatch)
    assert a.mesh.unpair(b.mesh.id)
    bob = owner(b)
    job = send(bob, [a.mesh.id], [("late.txt", b"late")])
    wait_for(lambda: target_states(bob, job) in ({"Alpha": "failed"}, {"Alpha": "declined"}))
    got = target(b, job)
    assert got["code"] == "peer_unpaired"
    assert "Pair again" in got["reason"]
    assert b"late" not in tap.to(a)
    assert owner(a).state()["incoming"] == []


def test_pairing_again_works_with_new_keys(make_node: NodeFactory) -> None:
    a, b = paired(make_node)
    old = a.mesh.trust.get(b.mesh.id).key  # type: ignore[union-attr]
    assert a.mesh.unpair(b.mesh.id)
    wait_for(lambda: b.mesh.trust.get(a.mesh.id) is None)
    pair(a, b)
    assert a.mesh.trust.get(b.mesh.id).key != old  # type: ignore[union-attr]
    assert owner(b).state()["keys"]["unpaired_by"] == []  # paired again: nothing to say
    alice = owner(a)
    job = send(alice, [b.mesh.id], [("again.txt", b"again")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    assert upload(alice, job, 0, b"again")[0] == 200
    assert target(a, job)["authenticated"] is True


def test_history_stays_after_unpairing(make_node: NodeFactory) -> None:
    a, b = paired(make_node)
    alice = owner(a)
    job = send(alice, [b.mesh.id], [("kept.txt", b"kept")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    assert upload(alice, job, 0, b"kept")[0] == 200

    def names(node: Node) -> set[str]:
        return {e["title"] for e in owner(node)("GET", "/api/history")[1]["entries"]}

    wait_for(lambda: "kept.txt" in names(a) and "kept.txt" in names(b))
    assert a.mesh.unpair(b.mesh.id)
    wait_for(lambda: b.mesh.trust.get(a.mesh.id) is None)
    assert "kept.txt" in names(a)
    assert "kept.txt" in names(b)
    assert (Path(b.config.storage_dir) / "kept.txt").read_bytes() == b"kept"


def test_unpairing_drops_the_keys_of_transfers_under_way(make_node: NodeFactory) -> None:
    a, b = paired(make_node)
    alice = owner(a)
    job = send(alice, [b.mesh.id], [("open.bin", b"x" * 1000)])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    assert a.mesh.unpair(b.mesh.id)
    delivery = a.mesh._jobs[job].deliveries[b.mesh.id]
    assert delivery.enc_base == b""
    wait_for(lambda: all(not s.enc_base for s in b.mesh._incoming.values()))
    _, result = upload(alice, job, 0, b"x" * 1000)
    assert not any(t.get("ok") for t in result.get("targets", {}).values()), result
    assert not (Path(b.config.storage_dir) / "open.bin").exists()
