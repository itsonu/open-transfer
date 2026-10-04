"""Pairing (SRP, confirmation, errors) and the identity it protects, on real nodes.

See docs/trust-model.md. Each test names the attack or failure it covers.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from open_transfer import mesh as mesh_module
from open_transfer import srp
from open_transfer.config import Config
from open_transfer.devices import session_mac
from open_transfer.node import Node
from tests import test_mesh
from tests.test_mesh import (
    NodeFactory,
    connect,
    device_id,
    owner,
    pair_nodes,
    send,
    target_states,
    upload,
    wait_for,
)

Json = dict[str, Any]
make_node = test_mesh.make_node  # the shared pytest fixture


def paired(client: test_mesh.Client, name: str) -> bool:
    return any(d["name"] == name and d["paired"] for d in client.state()["devices"])


def error(result: tuple[int, Any]) -> tuple[int, str]:
    status, data = result
    return status, (data.get("error") or {}).get("code", "")


# --------------------------------------------------------------- happy paths


def test_pair_by_code_and_address_then_transfers_auto_accept_both_ways(
    make_node: NodeFactory,
) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    alice, bob = owner(a), owner(b)
    status, data = pair_nodes(a, b)
    assert status == 200, data
    assert data["device"]["name"] == "Bravo"
    assert paired(alice, "Bravo")
    wait_for(lambda: paired(bob, "Alpha"))

    job = send(alice, [device_id(alice, "Bravo")], [("auto.txt", b"trusted")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    assert upload(alice, job, 0, b"trusted")[0] == 200
    assert (Path(b.config.storage_dir) / "auto.txt").read_bytes() == b"trusted"
    job = send(bob, [device_id(bob, "Alpha")], [("back.txt", b"hi")])
    wait_for(lambda: target_states(bob, job) == {"Alpha": "accepted"})


def test_a_device_visible_over_http_pairs_by_code_alone_without_multicast(
    make_node: NodeFactory,
) -> None:
    """The real-device bug: visible to the Mac, yet code entry said code_not_found."""
    a, b = make_node("Alpha"), make_node("Bravo")  # discovery off: no multicast at all
    connect(a, b)  # known only over HTTP
    status, data = pair_nodes(a, b, address=False)
    assert status == 200, data
    assert data["device"]["name"] == "Bravo"


def test_pair_with_a_chosen_device(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    assert pair_nodes(a, b, address=False, device=True)[0] == 200


def test_already_paired(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    assert pair_nodes(a, b)[0] == 200
    assert error(pair_nodes(a, b)) == (409, "already_paired")


# ------------------------------------------------------------ honest errors


def test_code_alone_with_nobody_around_says_discovery_is_unavailable(
    make_node: NodeFactory,
) -> None:
    a = make_node("Alpha")
    status, data = owner(a)("POST", "/api/pair", {"code": "123456"})
    assert (status, data["error"]["code"]) == (404, "discovery_unavailable")
    assert data["error"]["action"]


def test_devices_around_but_none_showing_a_code(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    status, data = owner(a)("POST", "/api/pair", {"code": "123456"})
    assert (status, data["error"]["code"]) == (404, "pairing_not_open")


def test_the_chosen_device_isnt_showing_a_code(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    status, data = owner(a)(
        "POST", "/api/pair", {"code": "123456", "address": f"127.0.0.1:{b.port}"}
    )
    assert (status, data["error"]["code"]) == (409, "pairing_not_open")


def test_unreachable_address(make_node: NodeFactory) -> None:
    a = make_node("Alpha")
    status, data = owner(a)("POST", "/api/pair", {"code": "123456", "address": "127.0.0.1:9"})
    assert (status, data["error"]["code"]) == (502, "device_unreachable")


# ------------------------------------------------------------- confirmation


def test_deny_means_nobody_trusts_anybody(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    assert error(pair_nodes(a, b, allow=False)) == (403, "trust_rejected")
    assert not paired(owner(a), "Bravo")
    assert not paired(owner(b), "Alpha")


def test_nobody_answering_the_prompt_is_a_rejection(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mesh_module, "PAIR_CONFIRM_TTL", 1.0)
    a, b = make_node("Alpha"), make_node("Bravo")
    bob = owner(b)
    code = bob("POST", "/api/pair/open")[1]["code"]
    status, data = owner(a)("POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{b.port}"})
    assert (status, data["error"]["code"]) == (403, "trust_rejected")
    assert not paired(bob, "Alpha")


def test_a_matching_code_alone_never_creates_trust(make_node: NodeFactory) -> None:
    """Until Bravo's owner presses Allow, Bravo trusts nobody."""
    a, b = make_node("Alpha"), make_node("Bravo")
    bob = owner(b)
    code = bob("POST", "/api/pair/open")[1]["code"]
    worker = threading.Thread(
        target=owner(a),
        args=("POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{b.port}"}),
    )
    worker.start()
    wait_for(lambda: bob.state().get("pair_requests"))
    assert not paired(bob, "Alpha")
    assert not b.mesh.trust.ids()
    request = bob.state()["pair_requests"][0]
    assert request["device"]["name"] == "Alpha"
    bob("POST", f"/api/pair/requests/{request['id']}/deny")
    worker.join(30)


# ------------------------------------------------------- guessing the code


def test_wrong_codes_are_refused_then_rate_limited(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    real = owner(b)("POST", "/api/pair/open")[1]["code"]
    wrong = f"{(int(real) + 1) % 10**6:06d}"
    results = [error(pair_nodes(a, b, code=wrong)) for _ in range(6)]
    assert results[:5] == [(403, "pairing_code_invalid")] * 5
    assert results[5] == (429, "rate_limited")
    assert not b.mesh.trust.ids()


def test_retrying_while_rate_limited_does_not_extend_the_block(make_node: NodeFactory) -> None:
    """Attempts refused by the limiter end there; once it lapses, the right code works."""
    a, b = make_node("Alpha"), make_node("Bravo")
    real = owner(b)("POST", "/api/pair/open")[1]["code"]
    wrong = f"{(int(real) + 1) % 10**6:06d}"
    for _ in range(5):
        pair_nodes(a, b, code=wrong)
    for _ in range(6):  # an impatient user keeps trying during the minute
        assert error(pair_nodes(a, b, code=wrong))[0] == 429
    b.mesh._pair_limiter.reset("pair:127.0.0.1")  # the minute has passed
    assert pair_nodes(a, b)[0] == 200


def test_the_window_closing_mid_attempt_expires_it(make_node: NodeFactory) -> None:
    b = make_node("Bravo")
    b.mesh.open_pairing()
    begun = b.mesh.pair_begin({"id": "d-" + "a" * 24, "name": "Mallory"}, "10.0.0.9")
    client = srp.Client(f"{b.mesh.id}|{'d-' + 'a' * 24}", b.mesh.pair_code)
    proof = client.respond(bytes.fromhex(begun["salt"]), int(begun["b"], 16))
    b.mesh.close_pairing()
    with pytest.raises(mesh_module.MeshError) as caught:
        b.mesh.pair_prove(begun["session"], format(client.a_pub, "x"), proof.hex(), "10.0.0.9")
    assert caught.value.code == "pairing_expired"


def test_replayed_and_altered_handshakes_are_refused(make_node: NodeFactory) -> None:
    b = make_node("Bravo")
    mallory = "d-" + "a" * 24
    b.mesh.open_pairing()
    code = b.mesh.pair_code
    begun = b.mesh.pair_begin({"id": mallory, "name": "Mallory"}, "10.0.0.9")
    client = srp.Client(f"{b.mesh.id}|{mallory}", code)
    proof = client.respond(bytes.fromhex(begun["salt"]), int(begun["b"], 16))
    tampered = bytes([proof[0] ^ 1]) + proof[1:]
    with pytest.raises(mesh_module.MeshError) as caught:  # altered proof
        b.mesh.pair_prove(begun["session"], format(client.a_pub, "x"), tampered.hex(), "10.0.0.9")
    assert caught.value.code == "pairing_code_invalid"
    with pytest.raises(mesh_module.MeshError) as caught:  # replaying the session afterwards
        b.mesh.pair_prove(begun["session"], format(client.a_pub, "x"), proof.hex(), "10.0.0.9")
    assert caught.value.code == "pairing_expired"
    with pytest.raises(mesh_module.MeshError):  # polling without the session key
        b.mesh.pair_status(begun["session"], session_mac(b"x" * 32, "status", begun["session"]))
    assert not b.mesh.trust.ids()


def test_a_session_only_works_from_where_it_started(make_node: NodeFactory) -> None:
    b = make_node("Bravo")
    mallory = "d-" + "a" * 24
    b.mesh.open_pairing()
    begun = b.mesh.pair_begin({"id": mallory, "name": "Mallory"}, "10.0.0.9")
    client = srp.Client(f"{b.mesh.id}|{mallory}", b.mesh.pair_code)
    proof = client.respond(bytes.fromhex(begun["salt"]), int(begun["b"], 16))
    with pytest.raises(mesh_module.MeshError):
        b.mesh.pair_prove(begun["session"], format(client.a_pub, "x"), proof.hex(), "10.0.0.66")


def test_nothing_on_the_wire_reveals_the_code(make_node: NodeFactory) -> None:
    """The multicast find carries no code data, and an SRP transcript can't be
    checked offline: even the right code doesn't reproduce the proof without
    the client's secret."""
    b = make_node("Bravo")
    sent: list[Json] = []
    b.mesh.discovery = type(
        "Fake", (), {"working": True, "find": lambda self: sent.append({"type": "find"})}
    )()
    with pytest.raises(mesh_module.MeshError):
        b.mesh.pair_with("123456")
    assert sent == [{"type": "find"}]

    b.mesh.discovery = None
    b.mesh.open_pairing()
    code, peer = b.mesh.pair_code, "d-" + "a" * 24
    begun = b.mesh.pair_begin({"id": peer, "name": "Peer"}, "10.0.0.9")
    observed = srp.Client(f"{b.mesh.id}|{peer}", code).respond(
        bytes.fromhex(begun["salt"]), int(begun["b"], 16)
    )
    replay = srp.Client(f"{b.mesh.id}|{peer}", code).respond(
        bytes.fromhex(begun["salt"]), int(begun["b"], 16)
    )
    assert observed != replay
    assert code not in json.dumps(begun)


# ------------------------------------------------- identity after pairing


class _Impostor(BaseHTTPRequestHandler):
    """Answers like an Open Transfer app, but without the pair key."""

    claim: ClassVar[Json] = {}

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps(self.claim).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST

    def log_message(self, *args: Any) -> None:
        pass


def _impostor(claim: Json) -> ThreadingHTTPServer:
    handler = type("Impostor", (_Impostor,), {"claim": claim})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_forged_hello_announce_and_bye_cannot_hijack_a_paired_device(
    make_node: NodeFactory,
) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    assert pair_nodes(a, b)[0] == 200
    real = a.mesh._peers[b.mesh.id]
    real_address = real.address
    claim = {"id": b.mesh.id, "name": "Evil Bravo", "form": "phone", "platform": "android"}
    fake = _impostor({**claim, "port": 1})
    try:
        fake_port = fake.server_address[1]
        # 1. an unsigned hello from elsewhere, claiming to be Bravo
        request = urllib.request.Request(
            f"{a.local_url}/api/p2p/v1/hello",
            data=json.dumps({**claim, "port": fake_port}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(request, timeout=5).read()
        # 2. a multicast announce and a bye from another address
        a.mesh._on_packet({"type": "announce", **claim, "port": fake_port}, "127.0.0.2")
        a.mesh._on_packet({"type": "bye", "id": b.mesh.id}, "127.0.0.2")
        time.sleep(1.0)  # let the address checks run (and fail)
        peer = a.mesh._peers[b.mesh.id]
        assert peer.address == real_address
        assert peer.name == "Bravo"
        assert peer.online
        assert a.mesh.trust.get(b.mesh.id).name == "Bravo"  # type: ignore[union-attr]
    finally:
        fake.shutdown()
    # Files still go to the real Bravo.
    alice = owner(a)
    job = send(alice, [b.mesh.id], [("f.txt", b"hi")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    upload(alice, job, 0, b"hi")
    assert (Path(b.config.storage_dir) / "f.txt").read_bytes() == b"hi"


def test_connecting_to_an_impostor_address_is_refused(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    assert pair_nodes(a, b)[0] == 200
    fake = _impostor({"id": b.mesh.id, "name": "Bravo", "port": 1})
    try:
        status, data = owner(a)(
            "POST", "/api/devices", {"address": f"127.0.0.1:{fake.server_address[1]}"}
        )
        assert (status, data["error"]["code"]) == (502, "identity_mismatch")
    finally:
        fake.shutdown()


def test_a_paired_device_that_really_moved_is_followed(
    make_node: NodeFactory, tmp_path: Path
) -> None:
    """Same device, new address (restart on another port; on Wi-Fi a new IP):
    it proves the pair key, so the address is updated."""
    a, b = make_node("Alpha"), make_node("Bravo")
    assert pair_nodes(a, b)[0] == 200
    old = a.mesh._peers[b.mesh.id].address
    b.stop()
    moved = Node(
        Config(
            storage_dir=tmp_path / "Bravo",
            port=0,
            device_name="Bravo",
            reserve_disk_bytes=0,
            discovery=False,
        )
    )
    moved.start()
    try:
        connect(moved, a)  # its hello to Alpha is signed: proof enough
        wait_for(lambda: a.mesh._peers[b.mesh.id].address != old)
        assert a.mesh._peers[b.mesh.id].port == moved.port
        alice = owner(a)
        job = send(alice, [b.mesh.id], [("moved.txt", b"hi")])
        wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    finally:
        moved.stop()


def test_an_unpaired_device_claiming_a_paired_id_still_needs_accept(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    assert pair_nodes(a, b)[0] == 200
    forged = {
        "from": {"id": a.mesh.id, "name": "Alpha", "port": a.port},
        "origin": {"id": a.mesh.id, "name": "Alpha"},
        "to": b.mesh.id,
        "files": [{"name": "evil.exe", "size": 3}],
    }
    request = urllib.request.Request(
        f"{b.local_url}/api/p2p/v1/offers",
        data=json.dumps(forged).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-OT-From": a.mesh.id, "X-OT-Auth": "1:00"},
    )
    with urllib.request.urlopen(request, timeout=5) as res:
        assert json.loads(res.read())["state"] == "pending"  # asks instead of auto-accepting


# ------------------------------------------------------------ unpairing


def test_unpair_is_mutual_and_old_keys_stop_working(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    assert pair_nodes(a, b)[0] == 200
    old_key = a.mesh.trust.get(b.mesh.id).key  # type: ignore[union-attr]
    assert owner(a)("DELETE", f"/api/pairs/{b.mesh.id}")[0] == 200
    assert not a.mesh.trust.get(b.mesh.id)
    wait_for(lambda: not b.mesh.trust.get(a.mesh.id))  # told over the signed request
    assert old_key  # and nobody holds it any more: offers ask again
    alice = owner(a)
    job = send(alice, [b.mesh.id], [("f.txt", b"hi")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "waiting"})
    assert pair_nodes(a, b)[0] == 200  # re-pairing works


def test_unpairing_while_the_other_is_offline_is_cleaned_up_later(
    make_node: NodeFactory, tmp_path: Path
) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    assert pair_nodes(a, b)[0] == 200
    b.stop()
    owner(a)("DELETE", f"/api/pairs/{b.mesh.id}")
    back = Node(
        Config(
            storage_dir=tmp_path / "Bravo",
            port=b.port,
            device_name="Bravo",
            reserve_disk_bytes=0,
            discovery=False,
        )
    )
    back.start()
    try:
        assert back.mesh.trust.get(a.mesh.id)  # still thinks it's paired…
        back.mesh.trust.get(a.mesh.id).paired_at = 0.0  # type: ignore[union-attr]  # (and not just now)
        connect(back, a)  # …until its next signed request: "peer_unpaired"
        wait_for(lambda: not back.mesh.trust.get(a.mesh.id))
    finally:
        back.stop()


def test_history_survives_unpairing(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    assert pair_nodes(a, b)[0] == 200
    alice = owner(a)
    job = send(alice, [b.mesh.id], [("f.txt", b"hi")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    upload(alice, job, 0, b"hi")
    alice("DELETE", f"/api/pairs/{b.mesh.id}")
    _status, data = alice("GET", "/api/history")
    assert [i["transfer_id"] for i in data["items"]] == [job]


# --------------------------------------------- reinstall / ghost devices


def test_a_reinstalled_device_is_new_and_the_old_one_is_marked(
    make_node: NodeFactory, tmp_path: Path
) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    assert pair_nodes(a, b)[0] == 200
    b.stop()
    reinstalled = Node(  # same name, fresh state: a new id
        Config(
            storage_dir=tmp_path / "Bravo-new", port=0, device_name="Bravo",
            reserve_disk_bytes=0, discovery=False,
        )
    )  # fmt: skip
    reinstalled.start()
    try:
        connect(a, reinstalled)
        a.mesh._peers[b.mesh.id].last_seen = 0.0  # the old install is gone (normally after 15 s)
        wait_for(lambda: sum(1 for d in owner(a).state()["devices"] if d["name"] == "Bravo") == 2)
        devices = [d for d in owner(a).state()["devices"] if d["name"] == "Bravo"]
        old = next(d for d in devices if d["id"] == b.mesh.id)
        new = next(d for d in devices if d["id"] == reinstalled.mesh.id)
        assert old.get("stale") is True
        assert not new.get("stale")
        assert old["short_id"] != new["short_id"]
        assert old["paired"]
        assert not new["paired"]
        assert owner(a)("DELETE", f"/api/devices/{b.mesh.id}")[0] == 200
        names = [d["id"] for d in owner(a).state()["devices"] if d["name"] == "Bravo"]
        assert names == [reinstalled.mesh.id]
    finally:
        reinstalled.stop()


# --------------------------------------------------------- QR and browsers


def test_qr_holds_only_temporary_pairing_data(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = make_node("Alpha", pin="4321")
    seen: list[str] = []
    import segno

    real_make = segno.make
    monkeypatch.setattr(
        segno, "make", lambda url, **kw: (seen.append(url), real_make(url, **kw))[1]
    )
    code = owner(a)("POST", "/api/pair/open")[1]["code"]
    assert owner(a)("GET", "/api/pair/qr.svg", raw=True)[0] == 200
    assert seen == [f"http://{seen[0].split('//')[1].split('/')[0]}/?pair={code}&id={a.mesh.id}"]
    assert "4321" not in seen[0]


def test_a_browser_scanning_the_qr_needs_the_window_open(make_node: NodeFactory) -> None:
    a = make_node("Alpha")
    code = owner(a).state()["pairing"]["code"]  # window closed: the code isn't accepted
    phone = test_mesh.visitor(a, "Phone")
    phone("GET", f"/?pair={code}", raw=True)
    assert phone.state()["me"]["paired"] is False
    code = owner(a)("POST", "/api/pair/open")[1]["code"]
    phone("GET", f"/?pair={code}", raw=True)
    assert phone.state()["me"]["paired"] is True


def test_a_just_made_pairing_survives_the_moment_before_both_sides_store_the_key(
    make_node: NodeFactory,
) -> None:
    """The device showing the code stores the key first; a hello it signs before
    the other side has stored it gets "unpaired" back. That must not undo it."""
    a, b = make_node("Alpha"), make_node("Bravo")
    assert pair_nodes(a, b)[0] == 200
    b.mesh._peer_unpaired(a.mesh.id)  # what that early "X-OT-Unpaired" answer triggers
    assert b.mesh.trust.get(a.mesh.id)
    b.mesh.trust.get(a.mesh.id).paired_at = 0.0  # type: ignore[union-attr]  # an old pairing…
    b.mesh._peer_unpaired(a.mesh.id)
    assert not b.mesh.trust.get(a.mesh.id)  # …is dropped


def test_code_alone_finds_the_new_device_not_one_already_paired(make_node: NodeFactory) -> None:
    """CI found this with 3 devices: a paired device whose Add device screen was
    still open got picked instead of the one showing the code ("already paired")."""
    a, b, c = make_node("Alpha"), make_node("Bravo"), make_node("Charlie")
    connect(a, b)
    connect(a, c)
    assert pair_nodes(a, b, address=False)[0] == 200
    owner(b)("POST", "/api/pair/open")  # Bravo's window is still open…
    status, data = pair_nodes(a, c, address=False)  # …but Alpha types Charlie's code
    assert status == 200, data
    assert data["device"]["name"] == "Charlie"


def test_pairing_again_and_again_from_one_address_is_not_rate_limited(
    make_node: NodeFactory,
) -> None:
    """Only wrong codes and attempts in progress count, not finished pairings."""
    a, b = make_node("Alpha"), make_node("Bravo")
    for _ in range(7):
        assert pair_nodes(a, b)[0] == 200
        owner(a)("DELETE", f"/api/pairs/{b.mesh.id}")
        wait_for(lambda: not b.mesh.trust.get(a.mesh.id))
