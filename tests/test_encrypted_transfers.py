"""Files between paired apps travel encrypted (ot-file/1, docs/encryption.md).

A wiretap on ``http.client`` records every byte an app sends to another app's
port: what anyone on the Wi-Fi would see.
"""

from __future__ import annotations

import hashlib
import http.client
import os
from pathlib import Path
from typing import Any

import pytest

from open_transfer import filecrypt
from open_transfer.node import Node
from tests.conftest import NodeFactory, Tap
from tests.test_mesh import (
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
from tests.test_moves import pair


def paired(make_node: NodeFactory) -> tuple[Node, Node]:
    a, b = make_node("Alpha"), make_node("Bravo")
    pair(a, b)
    # Bravo's signed hello says it speaks ot-file/1; from then on names are hidden too.
    wait_for(lambda: a.mesh.trust.get(b.mesh.id).enc)  # type: ignore[union-attr]
    return a, b


def target(node: Node, job: str) -> dict[str, Any]:
    for item in owner(node).state()["outgoing"]:
        if item["id"] == job:
            return item["targets"][0]  # type: ignore[no-any-return]
    raise AssertionError(job)


def seen(tap: Tap, node: Node, data: bytes) -> bool:
    wire = tap.to(node)
    return bool(data) and any(
        data[i : i + 64] in wire for i in range(0, max(1, len(data) - 64), 997)
    )


# -------------------------------------------------------------------- paired


@pytest.mark.parametrize("size", [0, 1_500_000, 5_000_000])  # empty, one PUT, 2 MiB pieces
def test_a_file_between_paired_apps_crosses_the_network_encrypted(
    make_node: NodeFactory, tap: Tap, size: int
) -> None:
    a, b = paired(make_node)
    alice = owner(a)
    payload = os.urandom(size)
    job = send(alice, [b.mesh.id], [("tax-return-2026.pdf", payload)])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    if size > 4 * 1024 * 1024:
        result = send_in_chunks(alice, job, payload, "up1")
    else:
        status, result = upload(alice, job, 0, payload)
        assert status == 200, result
    assert all(t["ok"] for t in result["targets"].values()), result
    assert (Path(b.config.storage_dir) / "tax-return-2026.pdf").read_bytes() == payload
    wait_for(lambda: target_states(alice, job) == {"Bravo": "done"})

    assert not seen(tap, b, payload), "file bytes crossed the network in plain text"
    assert b"tax-return" not in tap.to(b), "the file name crossed the network in plain text"
    assert hashlib.sha256(payload).hexdigest().encode() not in tap.to(b)
    assert target(a, job)["encrypted"] is True
    assert target(a, job)["verified"] is True
    got = incoming(owner(b), "done")
    assert got["encrypted"] is True
    assert got["files"][0]["name"] == "tax-return-2026.pdf"
    assert got["files"][0]["verified"] is True


def test_every_receiver_in_a_group_gets_its_own_key(make_node: NodeFactory, tap: Tap) -> None:
    a, b = paired(make_node)
    c = make_node("Charlie")
    pair(a, c)
    wait_for(lambda: a.mesh.trust.get(c.mesh.id).enc)  # type: ignore[union-attr]
    alice = owner(a)
    payload = os.urandom(300_000)
    job = send(alice, [b.mesh.id, c.mesh.id], [("group.bin", payload)])
    wait_for(lambda: set(target_states(alice, job).values()) == {"accepted"})
    status, result = upload(alice, job, 0, payload)
    assert status == 200, result
    assert all(t["ok"] for t in result["targets"].values()), result
    for node in (b, c):
        assert (Path(node.config.storage_dir) / "group.bin").read_bytes() == payload
        assert not seen(tap, node, payload)
    # The same file, sealed for two receivers, shares no ciphertext.
    record = 70_000
    assert tap.to(b)[-record:] not in tap.to(c)


def test_a_tampered_record_is_refused_and_nothing_is_kept(make_node: NodeFactory, tap: Tap) -> None:
    a, b = paired(make_node)
    alice = owner(a)
    payload = os.urandom(1_000_000)
    job = send(alice, [b.mesh.id], [("contract.pdf", payload)])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    tap.flip_at[b.port] = 1_000  # someone on the LAN changes one bit in transit
    _, result = upload(alice, job, 0, payload)
    assert not any(t["ok"] for t in result["targets"].values()), result
    assert not (Path(b.config.storage_dir) / "contract.pdf").exists()
    assert not list((Path(b.config.storage_dir) / ".open-transfer" / "incoming").glob("*.part"))
    failed = incoming(owner(b), "failed")
    assert "damaged" in failed["files"][0]["error"]


def test_a_tampered_piece_is_refused_and_nothing_is_kept(make_node: NodeFactory, tap: Tap) -> None:
    a, b = paired(make_node)
    alice = owner(a)
    payload = os.urandom(5_000_000)
    job = send(alice, [b.mesh.id], [("film.mp4", payload)])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    tap.flip_at[b.port] = 1_000_000
    result = send_in_chunks(alice, job, payload, "up2")
    assert not any(t["ok"] for t in result["targets"].values()), result
    assert not (Path(b.config.storage_dir) / "film.mp4").exists()
    assert "damaged" in incoming(owner(b), "failed")["files"][0]["error"]


def test_a_downgrade_to_plain_text_is_refused(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Someone in the middle strips the receiver's "enc": 1 from its answer.
    a, b = paired(make_node)
    real = a.mesh._http

    def strip(peer: Any, method: str, path: str, **kw: Any) -> tuple[int, dict[str, Any]]:
        status, data = real(peer, method, path, **kw)
        data.pop("enc", None)
        return status, data

    monkeypatch.setattr(a.mesh, "_http", strip)
    alice = owner(a)
    job = send(alice, [b.mesh.id], [("secret.txt", b"plans")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "failed"})
    assert "encryption" in target(a, job)["reason"]
    assert not (Path(b.config.storage_dir) / "secret.txt").exists()


def test_the_receiver_refuses_plain_bytes_for_an_encrypted_transfer(
    make_node: NodeFactory,
) -> None:
    a, b = paired(make_node)
    salt = bytes.fromhex(filecrypt.new_salt())
    pair_key = bytes.fromhex(a.mesh.trust.get(b.mesh.id).key)  # type: ignore[union-attr]
    ids = {"sender": a.mesh.id, "receiver": b.mesh.id, "job": "", "target": b.mesh.id}
    eph = filecrypt.Handshake(pair_key, salt, **ids).offer(b.mesh.kx_public)
    status, offer = a.mesh._http(
        a.mesh._peers[b.mesh.id],
        "POST",
        "/api/p2p/v1/offers",
        body={
            "from": a.mesh.self_info(include_visitors=False),
            "origin": {"id": a.mesh.id, "name": "Alpha"},
            "to": b.mesh.id,
            "files": [{"name": "x.txt", "size": 5}],
            "enc": {
                "v": filecrypt.VERSION,
                "salt": salt.hex(),
                "eph": eph.hex(),
                "kx": b.mesh.kx_public.hex(),
            },
        },
    )
    assert status == 201, offer
    assert offer["enc"] == filecrypt.VERSION
    conn = http.client.HTTPConnection("127.0.0.1", b.port, timeout=5)
    conn.request(
        "PUT",
        f"/api/p2p/v1/offers/{offer['id']}/files/0",
        body=b"plain",
        headers={"X-OT-Secret": offer["secret"], "Content-Length": "5"},
    )
    assert conn.getresponse().status == 400
    assert not (Path(b.config.storage_dir) / "x.txt").exists()


# ------------------------------------------- unpaired: one-off keys (Phase J)


def test_unpaired_apps_encrypt_with_one_off_keys(make_node: NodeFactory, tap: Tap) -> None:
    """X25519 to the receiver's per-run key: a listener reads nothing; nobody is verified."""
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice, bob = owner(a), owner(b)
    payload = os.urandom(200_000)
    job = send(alice, [device_id(alice, "Bravo")], [("open.bin", payload)])
    offer = incoming(bob)
    assert offer["encrypted"] is True
    assert offer["authenticated"] is False  # unpaired: "encrypted, not verified"
    assert offer["files"][0]["name"] == "open.bin"  # Bravo sees the real name...
    bob("POST", f"/api/incoming/{offer['id']}/accept")
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    assert upload(alice, job, 0, payload)[0] == 200
    assert (Path(b.config.storage_dir) / "open.bin").read_bytes() == payload
    assert target(a, job)["encrypted"] is True
    assert target(a, job)["authenticated"] is False
    assert not seen(tap, b, payload)
    assert b"open.bin" not in tap.to(b)  # ...the Wi-Fi doesn't
    done = incoming(bob, "done")
    assert done["files"][0]["verified"]


def test_paired_transfers_are_authenticated(make_node: NodeFactory) -> None:
    a, b = paired(make_node)
    alice = owner(a)
    job = send(alice, [b.mesh.id], [("p.txt", b"p")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    assert target(a, job)["authenticated"] is True
    assert incoming(owner(b), "accepted")["authenticated"] is True


def test_an_older_unpaired_app_gets_plain_files_labelled(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch, tap: Tap
) -> None:
    a, b = make_node("Alpha", encrypted_only=False), make_node("Bravo", encrypted_only=False)
    real_info = b.mesh.self_info  # from before Phase J: no per-run key to encrypt to
    monkeypatch.setattr(
        b.mesh, "self_info", lambda **kw: {k: v for k, v in real_info(**kw).items() if k != "kx"}
    )
    connect(a, b)
    alice, bob = owner(a), owner(b)
    payload = os.urandom(200_000)
    job = send(alice, [device_id(alice, "Bravo")], [("open.bin", payload)])
    offer = incoming(bob)
    assert offer["encrypted"] is False
    bob("POST", f"/api/incoming/{offer['id']}/accept")
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    assert upload(alice, job, 0, payload)[0] == 200
    assert target(a, job)["encrypted"] is False
    assert seen(tap, b, payload)  # the tap works: plain HTTP is readable


def test_stripping_the_one_off_key_is_refused(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bravo advertised a key; an offer answered without encryption means someone in between."""
    a, b = make_node("Alpha", encrypted_only=False), make_node("Bravo", encrypted_only=False)
    connect(a, b)
    real = b.mesh.open_offer

    def stripped(sender: str, target: str, job: str, enc: Any, **kw: Any) -> Any:
        return real(sender, target, job, None, **kw)  # as if "enc" never arrived

    monkeypatch.setattr(b.mesh, "open_offer", stripped)
    alice = owner(a)
    job = send(alice, [device_id(alice, "Bravo")], [("s.txt", b"s")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "failed"})
    assert "encryption" in target(a, job)["reason"]


def test_a_one_off_key_for_another_run_is_refused(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice = owner(a)
    bravo = device_id(alice, "Bravo")
    monkeypatch.setattr(a.mesh, "_upsert_peer", lambda *_, **__: None)  # no hello fixes it first
    with a.mesh._lock:
        peer = a.mesh._peers[bravo]
    peer.kx = filecrypt.new_kx()[1].hex()  # Bravo restarted: not the key it holds now
    job = send(alice, [bravo], [("k.txt", b"k")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "failed"})
    assert not (Path(b.config.storage_dir) / "k.txt").exists()


def unpaired_accepting(make_node: NodeFactory, *names: str) -> list[Node]:
    """Alpha plus unpaired receivers that accept by themselves (--auto-accept)."""
    a = make_node("Alpha")
    others = [make_node(n, auto_accept=True) for n in names]
    for other in others:
        connect(a, other)
    return [a, *others]


def test_unpaired_group_send_gives_each_receiver_its_own_one_off_key(
    make_node: NodeFactory, tap: Tap
) -> None:
    a, b, c = unpaired_accepting(make_node, "Bravo", "Charlie")
    alice = owner(a)
    payload = os.urandom(300_000)
    ids = [device_id(alice, "Bravo"), device_id(alice, "Charlie")]
    job = send(alice, ids, [("both.bin", payload)])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted", "Charlie": "accepted"})
    _, result = upload(alice, job, 0, payload)
    assert all(t["ok"] for t in result["targets"].values()), result
    for node in (b, c):
        assert (Path(node.config.storage_dir) / "both.bin").read_bytes() == payload
        assert not seen(tap, node, payload)
    # Different one-off keys: the two ciphertexts share no 64-byte run.
    wire_b, wire_c = tap.to(b), tap.to(c)
    assert not any(wire_b[i : i + 64] in wire_c for i in range(0, 200_000, 4999))


def test_an_unpaired_big_file_goes_in_encrypted_pieces(make_node: NodeFactory, tap: Tap) -> None:
    a, b = unpaired_accepting(make_node, "Bravo")
    alice = owner(a)
    payload = os.urandom(5_000_000)  # 2 MiB resumable pieces
    job = send(alice, [device_id(alice, "Bravo")], [("big.bin", payload)])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    result = send_in_chunks(alice, job, payload, "unpaired-big")
    assert all(t["ok"] for t in result["targets"].values()), result
    assert (Path(b.config.storage_dir) / "big.bin").read_bytes() == payload
    assert not seen(tap, b, payload)


def test_unpaired_tampering_leaves_nothing(make_node: NodeFactory, tap: Tap) -> None:
    a, b = unpaired_accepting(make_node, "Bravo")
    alice = owner(a)
    payload = os.urandom(200_000)
    job = send(alice, [device_id(alice, "Bravo")], [("t.bin", payload)])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    tap.flip_at[b.port] = 1_000
    _, result = upload(alice, job, 0, payload)
    assert not any(t["ok"] for t in result["targets"].values()), result
    assert not (Path(b.config.storage_dir) / "t.bin").exists()
    assert not list((Path(b.config.storage_dir) / ".open-transfer" / "incoming").glob("*.part"))


def test_an_older_paired_app_gets_plain_files_labelled(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = make_node("Alpha", encrypted_only=False), make_node("Bravo", encrypted_only=False)
    # Bravo is from before encryption: it advertises neither, and ignores the "enc" block.
    real_info = b.mesh.self_info
    monkeypatch.setattr(
        b.mesh,
        "self_info",
        lambda **kw: {k: v for k, v in real_info(**kw).items() if k not in {"enc", "kx"}},
    )
    real = a.mesh._http

    def old(peer: Any, method: str, path: str, **kw: Any) -> tuple[int, dict[str, Any]]:
        if kw.get("body"):
            kw["body"] = {k: v for k, v in kw["body"].items() if k != "enc"}
        return real(peer, method, path, **kw)

    monkeypatch.setattr(a.mesh, "_http", old)
    pair(a, b)
    alice = owner(a)
    job = send(alice, [b.mesh.id], [("old.txt", b"hello")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    assert upload(alice, job, 0, b"hello")[0] == 200
    assert (Path(b.config.storage_dir) / "old.txt").read_bytes() == b"hello"
    assert target(a, job)["encrypted"] is False
    assert not a.mesh.trust.get(b.mesh.id).enc  # type: ignore[union-attr]


def test_a_discovery_announcement_doesnt_forget_the_peers_key(make_node: NodeFactory) -> None:
    # Multicast announcements are short (no "kx"); they used to wipe the key a hello had
    # given, and the next unpaired transfer then went plain.
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    bravo = device_id(owner(a), "Bravo")
    assert a.mesh._peers[bravo].kx == b.mesh.kx_public.hex()
    a.mesh._upsert_peer({**b.mesh._describe(), "type": "announce"}, "127.0.0.1")
    assert a.mesh._peers[bravo].kx == b.mesh.kx_public.hex()


def test_apps_advertise_ot_file_2(make_node: NodeFactory) -> None:
    a = make_node("Alpha")
    info = owner(a)("GET", "/api/p2p/v1/info")[1]
    assert info["enc"] == 2
    assert len(bytes.fromhex(info["kx"])) == 32  # X25519, new each run, for unpaired senders


# ---------------------------------------------------------------- lifecycle


def test_unpairing_stops_transfers_from_that_device(make_node: NodeFactory) -> None:
    a, b = paired(make_node)
    alice = owner(a)
    job = send(alice, [b.mesh.id], [("late.bin", os.urandom(100_000))])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    assert b.mesh.unpair(a.mesh.id)
    got = incoming(owner(b), "canceled")
    assert "paired" in got["reason"]
    _, result = upload(alice, job, 0, os.urandom(100_000))
    assert not any(t.get("ok") for t in result.get("targets", {}).values()), result
    assert not (Path(b.config.storage_dir) / "late.bin").exists()


def test_after_unpairing_the_old_key_is_never_used_again(make_node: NodeFactory, tap: Tap) -> None:
    a, b = paired(make_node)
    assert b.mesh.unpair(a.mesh.id)  # Bravo tells Alpha, which forgets it too
    wait_for(lambda: a.mesh.trust.get(b.mesh.id) is None)
    alice = owner(a)
    job = send(alice, [b.mesh.id], [("after.txt", b"after")])
    offer = incoming(owner(b))  # a stranger's offer now: asked, not auto-accepted
    assert offer["encrypted"] is True
    assert offer["authenticated"] is False
    assert target(a, job)["authenticated"] is False
    assert b"after" not in tap.to(b)


def test_re_pairing_gives_a_new_key_and_transfers_still_work(make_node: NodeFactory) -> None:
    a, b = paired(make_node)
    old = a.mesh.trust.get(b.mesh.id).key  # type: ignore[union-attr]
    assert a.mesh.unpair(b.mesh.id)
    b.mesh.unpair(a.mesh.id)
    pair(a, b)
    assert a.mesh.trust.get(b.mesh.id).key != old  # type: ignore[union-attr]
    alice = owner(a)
    job = send(alice, [b.mesh.id], [("again.txt", b"again")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    assert upload(alice, job, 0, b"again")[0] == 200
    assert (Path(b.config.storage_dir) / "again.txt").read_bytes() == b"again"
    assert target(a, job)["encrypted"] is True


# ------------------------------------------------- many files, many at once


def test_several_files_each_get_their_own_key(make_node: NodeFactory, tap: Tap) -> None:
    a, b = paired(make_node)
    alice = owner(a)
    files = [(f"part-{i}.bin", os.urandom(200_000)) for i in range(3)]
    job = send(alice, [b.mesh.id], files)
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    for index, (_, payload) in enumerate(files):
        assert upload(alice, job, index, payload)[0] == 200
    for name, payload in files:
        assert (Path(b.config.storage_dir) / name).read_bytes() == payload
        assert not seen(tap, b, payload)
    wait_for(lambda: target_states(alice, job) == {"Bravo": "done"})
    assert target(a, job)["verified"] is True


def test_concurrent_encrypted_transfers_dont_mix(make_node: NodeFactory) -> None:
    from concurrent.futures import ThreadPoolExecutor

    a, b = paired(make_node)
    alice = owner(a)
    files = {f"same-time-{i}.bin": os.urandom(1_000_000 + i) for i in range(4)}
    jobs = {name: send(alice, [b.mesh.id], [(name, data)]) for name, data in files.items()}
    for job in jobs.values():
        wait_for(lambda job=job: target_states(alice, job) == {"Bravo": "accepted"})
    with ThreadPoolExecutor(4) as pool:
        codes = list(pool.map(lambda n: upload(alice, jobs[n], 0, files[n])[0], files))
    assert codes == [200] * 4
    for name, data in files.items():
        assert (Path(b.config.storage_dir) / name).read_bytes() == data


def test_a_canceled_encrypted_transfer_leaves_nothing(make_node: NodeFactory) -> None:
    a, b = paired(make_node)
    alice, bob = owner(a), owner(b)
    payload = os.urandom(5_000_000)
    job = send(alice, [b.mesh.id], [("half.bin", payload)])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    piece = 2 * 1024 * 1024
    status, _ = alice(
        "PUT",
        f"/api/send/{job}/files/0",
        data=payload[:piece],
        headers={"Content-Range": f"bytes 0-{piece - 1}/{len(payload)}", "X-Upload-Id": "up3"},
    )
    assert status == 308
    assert alice("DELETE", f"/api/send/{job}")[0] == 200
    wait_for(lambda: incoming(bob, "canceled"))
    assert not (Path(b.config.storage_dir) / "half.bin").exists()
    assert not list((Path(b.config.storage_dir) / ".open-transfer" / "incoming").glob("*.part"))


# ------------------------------------------------ browsers behind their apps


def test_a_browser_sends_through_its_app_encrypted_between_the_apps(
    make_node: NodeFactory, tap: Tap
) -> None:
    from tests.test_mesh import visitor

    a, b = paired(make_node)
    phone = visitor(a, "Pixel 8")
    payload = os.urandom(300_000)
    job = send(phone, [b.mesh.id], [("from-phone.jpg", payload)])
    offer = incoming(owner(b))
    assert offer["from"]["via_name"] == "Alpha"
    assert offer["encrypted"] is True  # the app-to-app hop; the phone's own hop is plain HTTP
    owner(b)("POST", f"/api/incoming/{offer['id']}/accept")
    wait_for(lambda: target_states(phone, job) == {"Bravo": "accepted"})
    assert upload(phone, job, 0, payload)[0] == 200
    assert (Path(b.config.storage_dir) / "from-phone.jpg").read_bytes() == payload
    assert not seen(tap, b, payload)
    assert b"from-phone" not in tap.to(b)


def test_an_app_sends_to_a_browser_behind_a_paired_app_encrypted(
    make_node: NodeFactory, tap: Tap
) -> None:
    from tests.test_mesh import visitor

    a, b = paired(make_node)
    tab = visitor(b, "Galaxy Tab", form="tablet")
    alice = owner(a)
    tab_id = device_id(alice, "Galaxy Tab")
    payload = os.urandom(300_000)
    job = send(alice, [tab_id], [("for-tab.pdf", payload)])
    offer = incoming(tab)
    tab("POST", f"/api/incoming/{offer['id']}/accept")
    wait_for(lambda: target_states(alice, job) == {"Galaxy Tab": "accepted"})
    assert upload(alice, job, 0, payload)[0] == 200
    assert tab("GET", "/api/inbox/files/for-tab.pdf", raw=True) == (200, payload)
    assert not seen(tap, b, payload)  # Alpha → Bravo; Bravo → the tab is plain HTTP
