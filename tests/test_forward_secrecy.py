"""Forward secrecy between apps: each transfer has its own ephemeral keys.

Someone who recorded a transfer and later steals the pair key still can't read
it: the session key also needs the X25519 shared secret of two ephemeral keys
that existed only during the handshake (docs/encryption.md, "Forward secrecy").
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from open_transfer import filecrypt
from open_transfer.node import Node
from tests.conftest import NodeFactory, Tap
from tests.test_encrypted_transfers import paired, target
from tests.test_mesh import owner, send, target_states, upload, wait_for
from tests.test_moves import pair


class Handshakes:
    """What crossed the network for each offer, and (test-only) each session key."""

    def __init__(self) -> None:
        self.offers: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []
        self.sessions: list[bytes] = []
        self.answer_patch: Any = None  # someone in the middle rewriting answers


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> Handshakes:
    seen = Handshakes()
    real_finish = filecrypt.Handshake.finish

    def finish(self: filecrypt.Handshake, eph: bytes, confirm: bytes) -> bytes:
        session = real_finish(self, eph, confirm)
        seen.sessions.append(session)
        return session

    monkeypatch.setattr(filecrypt.Handshake, "finish", finish)
    from open_transfer import mesh as mesh_module

    real_http = mesh_module.Mesh._http

    def http(self: Any, peer: Any, method: str, path: str, **kw: Any) -> tuple[int, Any]:
        status, data = real_http(self, peer, method, path, **kw)
        if method == "POST" and path.endswith("/offers"):
            seen.offers.append(kw["body"]["enc"] if kw.get("body", {}).get("enc") else {})
            if seen.answer_patch is not None:
                data = seen.answer_patch(dict(data))
            seen.answers.append(dict(data))
        return status, data

    monkeypatch.setattr(mesh_module.Mesh, "_http", http)
    return seen


def put_body(wire: bytes, size: int, nth: int = 0) -> bytes:
    """The encrypted body of the ``nth`` single-PUT upload in a wiretap."""
    at = -1
    for _ in range(nth + 1):
        at = wire.index(b"PUT /api/p2p/v1/offers/", at + 1)
    start = wire.index(b"\r\n\r\n", at) + 4
    return wire[start : start + filecrypt.sealed_size(size)]


def decrypts(base: bytes, salt: bytes, a: Node, b: Node, body: bytes, size: int) -> bool:
    key = filecrypt.file_key(base, salt, a.mesh.id, b.mesh.id, b.mesh.id, 0, size)
    try:
        filecrypt.Cipher(key, size).decrypt_range(0, body)
    except filecrypt.DecryptError:
        return False
    return True


def sent(a: Node, b: Node, payload: bytes, name: str = "f.bin") -> str:
    alice = owner(a)
    job = send(alice, [b.mesh.id], [(name, payload)])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    assert upload(alice, job, 0, payload)[0] == 200
    wait_for(lambda: target_states(alice, job) == {"Bravo": "done"})
    return job


def test_a_stolen_pair_key_cant_decrypt_a_recorded_transfer(
    make_node: NodeFactory, tap: Tap, seen: Handshakes
) -> None:
    a, b = paired(make_node)
    payload = os.urandom(100_000)
    sent(a, b, payload)
    offer, answer = seen.offers[-1], seen.answers[-1]
    body = put_body(tap.to(b), len(payload))
    salt = bytes.fromhex(offer["salt"])

    # Control: the session key (gone from both apps now) does open it.
    assert decrypts(seen.sessions[-1], salt, a, b, body, len(payload))

    # The attacker: everything that crossed the network, plus the pair key.
    pair_key = bytes.fromhex(a.mesh.trust.get(b.mesh.id).key)  # type: ignore[union-attr]
    assert {"eph", "kx", "salt"} <= offer.keys()
    assert {"eph", "confirm"} <= answer.keys()
    psk = filecrypt.hkdf(pair_key, salt=salt, info=b"open-transfer-psk/2")
    for guess in (
        pair_key,  # keys derived straight from the pair key (ot-file/1)
        psk,
        filecrypt.hkdf(bytes(32) + psk, salt=salt, info=b"open-transfer-offer/2"),
    ):
        assert not decrypts(guess, salt, a, b, body, len(payload))
    # Neither app keeps what would be needed once the transfer is over.
    wait_for(lambda: all(not d.enc_base for j in a.mesh._jobs.values() for d in j.deliveries.values()))  # fmt: skip
    wait_for(lambda: all(not s.enc_base for s in b.mesh._incoming.values()))


def test_unpaired_transfers_have_forward_secrecy_too(
    make_node: NodeFactory, tap: Tap, seen: Handshakes
) -> None:
    from tests.test_encrypted_transfers import unpaired_accepting

    a, b = unpaired_accepting(make_node, "Bravo")
    payload = os.urandom(50_000)
    sent(a, b, payload)
    body = put_body(tap.to(b), len(payload))
    salt = bytes.fromhex(seen.offers[-1]["salt"])
    assert decrypts(seen.sessions[-1], salt, a, b, body, len(payload))
    assert target(a, sent(a, b, b"again"))["authenticated"] is False  # not verified, still


def test_every_transfer_and_retry_gets_fresh_ephemeral_keys(
    make_node: NodeFactory, seen: Handshakes
) -> None:
    a, b = paired(make_node)
    for i in range(3):  # a retry is a new offer
        sent(a, b, b"x" * (i + 1), name=f"r{i}.txt")
    assert len({o["eph"] for o in seen.offers}) == 3
    assert len({o["salt"] for o in seen.offers}) == 3
    assert len({x["eph"] for x in seen.answers}) == 3
    assert len(set(seen.sessions)) == 3


def test_each_receiver_in_a_group_has_its_own_session(
    make_node: NodeFactory, tap: Tap, seen: Handshakes
) -> None:
    a, b = paired(make_node)
    c = make_node("Charlie")
    pair(a, c)
    wait_for(lambda: a.mesh.trust.get(c.mesh.id).enc)  # type: ignore[union-attr]
    alice = owner(a)
    payload = os.urandom(80_000)
    job = send(alice, [b.mesh.id, c.mesh.id], [("g.bin", payload)])
    wait_for(lambda: set(target_states(alice, job).values()) == {"accepted"})
    assert upload(alice, job, 0, payload)[0] == 200
    assert len(seen.sessions) == 2
    assert len({o["eph"] for o in seen.offers}) == 2
    to_b, to_c = put_body(tap.to(b), len(payload)), put_body(tap.to(c), len(payload))
    assert to_b != to_c
    for session, offer in zip(seen.sessions, seen.offers, strict=True):
        salt = bytes.fromhex(offer["salt"])
        # One receiver's session opens its own copy and never the other's.
        opens_b = decrypts(session, salt, a, b, to_b, len(payload))
        opens_c = decrypts(session, salt, a, c, to_c, len(payload))
        assert opens_b != opens_c


@pytest.mark.parametrize(
    ("what", "patch"),
    [
        ("receiver's key swapped", lambda d: {**d, "eph": filecrypt.new_kx()[1].hex()}),
        ("confirmation forged", lambda d: {**d, "confirm": "00" * 32}),
        ("handshake stripped (a downgrade)", lambda d: {k: v for k, v in d.items() if k not in {"eph", "confirm"}}),
    ],
)  # fmt: skip
def test_a_changed_answer_stops_the_transfer(
    make_node: NodeFactory, seen: Handshakes, what: str, patch: Any
) -> None:
    a, b = paired(make_node)
    seen.answer_patch = patch
    alice = owner(a)
    job = send(alice, [b.mesh.id], [("s.txt", b"secret")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "failed"}, timeout=15)
    assert "encryption" in target(a, job)["reason"], what
    assert not (Path(b.config.storage_dir) / "s.txt").exists()


def test_an_answer_from_an_earlier_transfer_is_refused(
    make_node: NodeFactory, seen: Handshakes
) -> None:
    a, b = paired(make_node)
    sent(a, b, b"first", name="first.txt")
    old = seen.answers[-1]
    seen.answer_patch = lambda d: {**d, "eph": old["eph"], "confirm": old["confirm"]}
    alice = owner(a)
    job = send(alice, [b.mesh.id], [("second.txt", b"second")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "failed"}, timeout=15)
    assert "encryption" in target(a, job)["reason"]


def test_a_receiver_that_restarted_gets_a_fresh_handshake(
    make_node: NodeFactory, seen: Handshakes
) -> None:
    a, b = paired(make_node)
    sent(a, b, b"before", name="before.txt")
    # What a restart does to the receiver's keys: a new per-run key, no sessions.
    b.mesh._kx, b.mesh.kx_public = filecrypt.new_kx()
    with b.mesh._lock:
        b.mesh._incoming.clear()
    wait_for(lambda: a.mesh._peers[b.mesh.id].kx == b.mesh.kx_public.hex())  # its next hello
    sent(a, b, b"after", name="after.txt")
    assert (Path(b.config.storage_dir) / "after.txt").read_bytes() == b"after"
    assert seen.offers[-1]["kx"] == b.mesh.kx_public.hex()
    assert seen.sessions[-1] != seen.sessions[0]


def test_a_finished_session_cant_take_more_bytes(make_node: NodeFactory) -> None:
    a, b = paired(make_node)
    job = sent(a, b, b"done", name="done.txt")
    status, result = upload(owner(a), job, 0, b"more")
    assert status != 200 or not any(t.get("ok") for t in result.get("targets", {}).values())
