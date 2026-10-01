"""Nearby devices end to end: real nodes talking HTTP (and multicast) to each other."""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import random
import socket
import struct
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from open_transfer import network
from open_transfer.config import Config
from open_transfer.node import Node

Json = dict[str, Any]


class Client:
    """A tiny HTTP client with cookies (one per simulated browser)."""

    def __init__(self, base: str) -> None:
        self.base = base
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def __call__(
        self,
        method: str,
        path: str,
        body: Json | None = None,
        data: bytes | None = None,
        raw: bool = False,
    ) -> tuple[int, Any]:
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif data is not None:
            headers["Content-Type"] = "application/octet-stream"
        if method != "GET":
            headers["Origin"] = self.base  # like a browser on this page
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers
        )
        try:
            with self._opener.open(request, timeout=30) as res:
                payload = res.read()
                return res.status, payload if raw else json.loads(payload or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def state(self) -> Json:
        status, data = self("GET", "/api/state")
        assert status == 200, data
        return data  # type: ignore[no-any-return]


def wait_for(check: Callable[[], Any], timeout: float = 10.0, interval: float = 0.1) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for condition")
        time.sleep(interval)


NodeFactory = Callable[..., Node]


@pytest.fixture
def make_node(tmp_path: Path) -> Iterator[NodeFactory]:
    nodes: list[Node] = []
    discovery_port = random.randint(40000, 59000)

    def factory(name: str, **overrides: Any) -> Node:
        overrides.setdefault("discovery", False)
        overrides.setdefault("discovery_port", discovery_port)
        config = Config(
            storage_dir=tmp_path / name,
            port=0,
            device_name=name,
            reserve_disk_bytes=0,
            **overrides,
        )
        node = Node(config)
        node.start()
        nodes.append(node)
        return node

    yield factory
    for node in nodes:
        node.stop()


def owner(node: Node) -> Client:
    return Client(node.local_url)


def device_id(client: Client, name: str) -> str:
    def find() -> str | None:
        for device in client.state()["devices"]:
            if device["name"] == name and device["online"]:
                return str(device["id"])
        return None

    return str(wait_for(find))


def connect(a: Node, b: Node) -> None:
    status, data = owner(a)("POST", "/api/devices", {"address": f"127.0.0.1:{b.port}"})
    assert status == 200, data


def incoming(client: Client, state: str = "pending") -> Json:
    def find() -> Json | None:
        for item in client.state()["incoming"]:
            if item["state"] == state:
                return item  # type: ignore[no-any-return]
        return None

    return wait_for(find)  # type: ignore[no-any-return]


def target_states(client: Client, job_id: str) -> dict[str, str]:
    for job in client.state()["outgoing"]:
        if job["id"] == job_id:
            return {t["name"]: t["state"] for t in job["targets"]}
    return {}


def send(client: Client, to: list[str], files: list[tuple[str, bytes]]) -> str:
    status, data = client(
        "POST", "/api/send", {"to": to, "files": [{"name": n, "size": len(b)} for n, b in files]}
    )
    assert status == 201, data
    return str(data["job"]["id"])


def upload(client: Client, job_id: str, index: int, payload: bytes) -> tuple[int, Any]:
    return client("PUT", f"/api/send/{job_id}/files/{index}", data=payload)


def lan_ip() -> str:
    ip = network.primary_ip()
    if not ip:
        pytest.skip("needs a non-loopback network interface")
    return ip


def multicast_works(port: int) -> bool:
    group = "239.255.77.77"
    try:
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rx.bind(("", port))
        rx.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton("0.0.0.0")),
        )
        rx.settimeout(1.0)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        tx.sendto(b"probe", (group, port))
        return rx.recv(16) == b"probe"
    except OSError:
        return False
    finally:
        for sock in ("rx", "tx"):
            if sock in locals():
                locals()[sock].close()


# ------------------------------------------------------------------ owner ↔ owner


def test_send_to_one_device_after_accepting(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice, bob = owner(a), owner(b)
    payload = os.urandom(3_000_000)
    job = send(alice, [device_id(alice, "Bravo")], [("holiday.mov", payload)])

    offer = incoming(bob)
    assert offer["from"]["name"] == "Alpha"
    assert offer["files"][0] == {**offer["files"][0], "name": "holiday.mov", "size": len(payload)}
    wait_for(lambda: target_states(alice, job) == {"Bravo": "waiting"})
    assert bob("POST", f"/api/incoming/{offer['id']}/accept")[0] == 200
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})

    status, result = upload(alice, job, 0, payload)
    assert status == 200, result
    assert all(t["ok"] for t in result["targets"].values())
    assert (Path(b.config.storage_dir) / "holiday.mov").read_bytes() == payload
    wait_for(lambda: target_states(alice, job) == {"Bravo": "done"})
    assert incoming(bob, "done")["received"] == len(payload)


def test_declined_offer_sends_nothing(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice, bob = owner(a), owner(b)
    job = send(alice, [device_id(alice, "Bravo")], [("secret.txt", b"nope")])
    offer = incoming(bob)
    bob("POST", f"/api/incoming/{offer['id']}/decline")
    wait_for(lambda: target_states(alice, job) == {"Bravo": "declined"})
    status, data = upload(alice, job, 0, b"nope")
    assert status == 409
    assert data["error"]["code"] == "no_receivers"
    assert not (Path(b.config.storage_dir) / "secret.txt").exists()


def test_upload_waits_until_receivers_answer(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice = owner(a)
    job = send(alice, [device_id(alice, "Bravo")], [("a.txt", b"x")])
    incoming(owner(b))
    status, data = upload(alice, job, 0, b"x")
    assert status == 409
    assert data["error"]["code"] == "waiting"


def test_one_upload_reaches_many_devices(make_node: NodeFactory) -> None:
    a = make_node("Alpha")
    others = [make_node(name) for name in ("Bravo", "Charlie", "Delta")]
    for node in others:
        connect(a, node)
    alice = owner(a)
    ids = [device_id(alice, n.mesh.identity.name) for n in others]
    files = [("one.bin", os.urandom(1_500_000)), ("two.txt", b"second file")]
    job = send(alice, ids, files)
    for node in others:
        offer = incoming(owner(node))
        owner(node)("POST", f"/api/incoming/{offer['id']}/accept")
    wait_for(lambda: set(target_states(alice, job).values()) == {"accepted"})
    for index, (_, payload) in enumerate(files):
        status, result = upload(alice, job, index, payload)
        assert status == 200
        assert len(result["targets"]) == 3
        assert all(t["ok"] for t in result["targets"].values())
    for node in others:
        root = Path(node.config.storage_dir)
        for name, payload in files:
            assert (
                hashlib.sha256((root / name).read_bytes()).digest()
                == hashlib.sha256(payload).digest()
            )
    wait_for(lambda: set(target_states(alice, job).values()) == {"done"})


def test_partial_group_one_declines_others_receive(make_node: NodeFactory) -> None:
    a, b, c = make_node("Alpha"), make_node("Bravo"), make_node("Charlie")
    connect(a, b)
    connect(a, c)
    alice = owner(a)
    job = send(alice, [device_id(alice, "Bravo"), device_id(alice, "Charlie")], [("f.txt", b"hi")])
    owner(b)("POST", f"/api/incoming/{incoming(owner(b))['id']}/accept")
    owner(c)("POST", f"/api/incoming/{incoming(owner(c))['id']}/decline")
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted", "Charlie": "declined"})
    status, result = upload(alice, job, 0, b"hi")
    assert status == 200
    assert list(result["targets"]) == [device_id(alice, "Bravo")]
    assert (Path(b.config.storage_dir) / "f.txt").exists()
    assert not (Path(c.config.storage_dir) / "f.txt").exists()


def test_receiver_can_cancel_mid_transfer(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice, bob = owner(a), owner(b)
    job = send(alice, [device_id(alice, "Bravo")], [("big.bin", b"x" * 10)])
    offer = incoming(bob)
    bob("POST", f"/api/incoming/{offer['id']}/accept")
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    assert bob("DELETE", f"/api/incoming/{offer['id']}")[0] == 200
    status, result = upload(alice, job, 0, b"x" * 10)
    assert status == 200
    assert not result["targets"][device_id(alice, "Bravo")]["ok"]
    assert not (Path(b.config.storage_dir) / "big.bin").exists()
    wait_for(lambda: target_states(alice, job) == {"Bravo": "canceled"})


def test_sender_can_cancel_before_upload(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice, bob = owner(a), owner(b)
    job = send(alice, [device_id(alice, "Bravo")], [("a.txt", b"x")])
    offer = incoming(bob)
    assert alice("DELETE", f"/api/send/{job}")[0] == 200
    wait_for(
        lambda: any(
            i["id"] == offer["id"] and i["state"] == "canceled" for i in bob.state()["incoming"]
        )
    )
    assert bob("POST", f"/api/incoming/{offer['id']}/accept")[0] == 409


def test_offer_to_offline_device_fails_cleanly(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice = owner(a)
    bravo = device_id(alice, "Bravo")
    b.stop()
    job = send(alice, [bravo], [("a.txt", b"x")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "failed"})


def test_sizes_must_match_the_offer(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    alice, bob = owner(a), owner(b)
    job = send(alice, [device_id(alice, "Bravo")], [("a.txt", b"12345")])
    bob("POST", f"/api/incoming/{incoming(bob)['id']}/accept")
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    status, data = upload(alice, job, 0, b"123")
    assert status == 400
    assert data["error"]["code"] == "size_mismatch"


def test_paired_only_refuses_strangers(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo", paired_only=True)
    connect(a, b)
    alice = owner(a)
    job = send(alice, [device_id(alice, "Bravo")], [("a.txt", b"x")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "declined"})
    assert not owner(b).state()["incoming"]


def test_not_enough_space_is_declined_up_front(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo", max_upload_size=10)
    connect(a, b)
    alice = owner(a)
    job = send(alice, [device_id(alice, "Bravo")], [("a.bin", b"x" * 11)])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "declined"})


# ---------------------------------------------------------------------- pairing


def test_pairing_by_code_and_address_auto_accepts(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    alice, bob = owner(a), owner(b)
    code = bob.state()["pairing"]["code"]
    status, data = alice("POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{b.port}"})
    assert status == 200, data
    assert data["device"]["name"] == "Bravo"
    assert bob.state()["pairing"]["code"] != code  # each code works once
    devices = {d["name"]: d for d in alice.state()["devices"]}
    assert devices["Bravo"]["paired"] is True
    wait_for(lambda: any(d["name"] == "Alpha" and d["paired"] for d in bob.state()["devices"]))

    job = send(alice, [devices["Bravo"]["id"]], [("auto.txt", b"trusted")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    assert upload(alice, job, 0, b"trusted")[0] == 200
    assert (Path(b.config.storage_dir) / "auto.txt").read_bytes() == b"trusted"
    # …and in the other direction too.
    job = send(bob, [device_id(bob, "Alpha")], [("back.txt", b"hi")])
    wait_for(lambda: target_states(bob, job) == {"Alpha": "accepted"})


def test_wrong_pairing_code_is_rejected_and_rate_limited(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    alice = owner(a)
    real = owner(b).state()["pairing"]["code"]
    wrong = f"{(int(real) + 1) % 10**6:06d}"
    codes = []
    for _ in range(6):
        status, _ = alice("POST", "/api/pair", {"code": wrong, "address": f"127.0.0.1:{b.port}"})
        codes.append(status)
    assert codes[:5] == [403] * 5
    assert codes[5] == 429
    assert not any(d["paired"] for d in alice.state()["devices"])


def test_unpair_removes_trust(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    alice = owner(a)
    code = owner(b).state()["pairing"]["code"]
    alice("POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{b.port}"})
    bravo = device_id(alice, "Bravo")
    assert alice("DELETE", f"/api/pairs/{bravo}")[0] == 200
    assert not {d["id"]: d for d in alice.state()["devices"]}[bravo]["paired"]


def test_forged_signature_is_not_trusted(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    alice = owner(a)
    code = owner(b).state()["pairing"]["code"]
    alice("POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{b.port}"})
    # Someone else claims to be Alpha but can't sign with the pairing key.
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


# ------------------------------------------------------------------ browsers


def visitor(node: Node, name: str, form: str = "phone", platform: str = "android") -> Client:
    client = Client(f"http://{lan_ip()}:{node.port}")
    status, _ = client("POST", "/api/me", {"name": name, "form": form, "platform": platform})
    assert status == 200
    return client


def test_browser_visitor_sends_through_its_app_to_another_app(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    phone = visitor(a, "Pixel 8")
    me = phone.state()["me"]
    assert me["kind"] == "visitor"
    names = {d["name"]: d for d in phone.state()["devices"]}
    assert names["Alpha"]["host"] is True
    job = send(phone, [names["Bravo"]["id"]], [("photo.jpg", b"jpegdata")])
    offer = incoming(owner(b))
    assert offer["from"]["name"] == "Pixel 8"
    assert offer["from"]["via_name"] == "Alpha"
    owner(b)("POST", f"/api/incoming/{offer['id']}/accept")
    wait_for(lambda: target_states(phone, job) == {"Bravo": "accepted"})
    assert upload(phone, job, 0, b"jpegdata")[0] == 200
    assert (Path(b.config.storage_dir) / "photo.jpg").read_bytes() == b"jpegdata"
    assert not (Path(a.config.storage_dir) / "photo.jpg").exists()  # relayed, not stored


def test_app_sends_to_a_browser_visitor_of_another_app(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(b, a)
    phone = visitor(a, "Galaxy Tab", form="tablet")
    bob = owner(b)
    tab = device_id(bob, "Galaxy Tab")  # learned from Alpha's hello
    job = send(bob, [tab], [("report.pdf", b"%PDF-1.7")])
    offer = incoming(phone)
    assert offer["from"]["name"] == "Bravo"
    phone("POST", f"/api/incoming/{offer['id']}/accept")
    wait_for(lambda: target_states(bob, job) == {"Galaxy Tab": "accepted"})
    assert upload(bob, job, 0, b"%PDF-1.7")[0] == 200
    inbox = phone.state()["inbox"]
    assert [f["name"] for f in inbox] == ["report.pdf"]
    status, body = phone("GET", "/api/inbox/files/report.pdf", raw=True)
    assert (status, body) == (200, b"%PDF-1.7")
    # The app's owner folder never sees a visitor's inbox.
    assert not (Path(a.config.storage_dir) / "report.pdf").exists()


def test_browsers_on_the_same_app_send_to_each_other(make_node: NodeFactory) -> None:
    a = make_node("Alpha")
    phone, tablet = visitor(a, "Phone"), visitor(a, "Tablet", form="tablet")
    job = send(phone, [device_id(phone, "Tablet")], [("note.txt", b"hey")])
    offer = incoming(tablet)
    tablet("POST", f"/api/incoming/{offer['id']}/accept")
    wait_for(lambda: target_states(phone, job) == {"Tablet": "accepted"})
    assert upload(phone, job, 0, b"hey")[0] == 200
    assert tablet("GET", "/api/inbox/files/note.txt", raw=True) == (200, b"hey")
    # Only the recipient can read its inbox.
    assert phone("GET", "/api/inbox/files/note.txt")[0] == 404


def test_visitor_sends_to_the_app_owner(make_node: NodeFactory) -> None:
    a = make_node("Alpha")
    phone = visitor(a, "Phone")
    job = send(phone, [a.mesh.id], [("selfie.jpg", b"img")])
    offer = incoming(owner(a))
    owner(a)("POST", f"/api/incoming/{offer['id']}/accept")
    wait_for(lambda: target_states(phone, job) == {"Alpha": "accepted"})
    assert upload(phone, job, 0, b"img")[0] == 200
    assert (Path(a.config.storage_dir) / "selfie.jpg").read_bytes() == b"img"


def test_visitor_paired_by_qr_code_is_auto_accepted(make_node: NodeFactory) -> None:
    a = make_node("Alpha")
    code = owner(a).state()["pairing"]["code"]
    phone = Client(f"http://{lan_ip()}:{a.port}")
    phone("GET", f"/?pair={code}", raw=True)
    assert phone.state()["me"]["paired"] is True
    job = send(phone, [a.mesh.id], [("x.txt", b"x")])
    wait_for(lambda: target_states(phone, job) == {"Alpha": "accepted"})


def test_visitors_cannot_use_owner_controls_or_browse(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    phone = visitor(a, "Phone")
    assert "pairing" not in phone.state()
    assert phone("POST", "/api/pair", {"code": "123456"})[0] == 403
    assert phone("POST", "/api/devices", {"address": f"127.0.0.1:{b.port}"})[0] == 403
    assert phone("GET", "/api/files")[0] == 403
    assert phone("GET", "/api/pair/qr.svg")[0] == 403


def test_visitor_cannot_answer_someone_elses_offer(make_node: NodeFactory) -> None:
    a = make_node("Alpha")
    phone, other = visitor(a, "Phone"), visitor(a, "Other")
    send(owner(a), [device_id(owner(a), "Phone")], [("a.txt", b"x")])
    offer = incoming(phone)
    assert other("POST", f"/api/incoming/{offer['id']}/accept")[0] == 404


def test_visitor_leaving_is_noticed(make_node: NodeFactory) -> None:
    a = make_node("Alpha")
    visitor(a, "Phone")
    assert device_id(owner(a), "Phone")
    a.mesh._visitors[next(iter(a.mesh._visitors))].last_seen -= 60
    assert all(d["name"] != "Phone" for d in owner(a).state()["devices"])


# ------------------------------------------------------------------ discovery


def test_devices_find_each_other_automatically(make_node: NodeFactory) -> None:
    port = random.randint(40000, 59000)
    if not multicast_works(port) or not network.lan_ips():
        pytest.skip("multicast is not available here")
    a = make_node("Alpha", discovery=True, discovery_port=port)
    b = make_node("Bravo", discovery=True, discovery_port=port)
    c = make_node("Charlie", discovery=True, discovery_port=port)
    for node, others in (
        (a, {"Bravo", "Charlie"}),
        (b, {"Alpha", "Charlie"}),
        (c, {"Alpha", "Bravo"}),
    ):
        wait_for(
            lambda n=node, o=others: (
                o <= {d["name"] for d in owner(n).state()["devices"] if d["online"]}
            )
        )
    # Pair by code alone: Alpha finds whoever shows Bravo's code on the network.
    code = owner(b).state()["pairing"]["code"]
    status, data = owner(a)("POST", "/api/pair", {"code": code})
    assert status == 200, data
    assert data["device"]["name"] == "Bravo"
    # Leaving says goodbye, so others update straight away.
    c.stop()
    wait_for(
        lambda: (
            not any(d["name"] == "Charlie" and d["online"] for d in owner(a).state()["devices"])
        ),
        timeout=5,
    )


def test_state_supports_etags(make_node: NodeFactory) -> None:
    a = make_node("Alpha")
    request = urllib.request.Request(f"{a.local_url}/api/state")
    with urllib.request.urlopen(request) as res:
        etag = res.headers["ETag"]
    request = urllib.request.Request(f"{a.local_url}/api/state", headers={"If-None-Match": etag})
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(request)
    assert info.value.code == 304


# ------------------------------------------------ direct browser ↔ browser (WebRTC)


def _signals(client: Client) -> list[Json]:
    status, data = client("GET", "/api/signals")
    assert status == 200
    return data["signals"]  # type: ignore[no-any-return]


@pytest.mark.parametrize("same_app", [True, False])
def test_browsers_swap_connection_messages_through_their_apps(
    make_node: NodeFactory, same_app: bool
) -> None:
    a = make_node("Alpha")
    b = a if same_app else make_node("Bravo")
    if not same_app:
        connect(a, b)
    phone, tablet = visitor(a, "Phone"), visitor(b, "Tablet", form="tablet")
    job = send(phone, [device_id(phone, "Tablet")], [("clip.mp4", b"v" * 100)])
    offer = incoming(tablet)
    tablet("POST", f"/api/incoming/{offer['id']}/accept")
    tablet_id = tablet.state()["me"]["id"]
    wait_for(lambda: target_states(phone, job) == {"Tablet": "accepted"})

    assert (
        phone("POST", f"/api/send/{job}/targets/{tablet_id}/direct", {"state": "trying"})[0] == 200
    )
    assert (
        phone(
            "POST",
            "/api/signal",
            {"job": job, "target": tablet_id, "kind": "offer", "sdp": "v=0 offer"},
        )[0]
        == 200
    )
    got = wait_for(lambda: _signals(tablet))
    assert got == [{"kind": "offer", "sdp": "v=0 offer", "session": offer["id"]}]
    assert (
        tablet(
            "POST", "/api/signal", {"session": offer["id"], "kind": "answer", "sdp": "v=0 answer"}
        )[0]
        == 200
    )
    back = wait_for(lambda: _signals(phone))
    assert back == [{"kind": "answer", "sdp": "v=0 answer", "job": job, "target": tablet_id}]

    # While a direct attempt runs the app doesn't stream the file itself…
    assert upload(phone, job, 0, b"v" * 100)[1]["error"]["code"] == "no_receivers"
    # …the receiving page reports what arrived, the sender reports it's done.
    tablet(
        "POST", f"/api/incoming/{offer['id']}/direct", {"index": 0, "received": 100, "done": True}
    )
    phone("POST", f"/api/send/{job}/targets/{tablet_id}/direct", {"state": "done"})
    finished = incoming(tablet, "done")
    assert finished["files"][0]["direct"] is True
    wait_for(lambda: target_states(phone, job) == {"Tablet": "done"})
    assert tablet.state()["inbox"] == []  # it lives in the browser, not on the app


def test_failed_direct_attempt_falls_back_to_the_app(make_node: NodeFactory) -> None:
    a = make_node("Alpha")
    phone, tablet = visitor(a, "Phone"), visitor(a, "Tablet")
    job = send(phone, [device_id(phone, "Tablet")], [("doc.pdf", b"pdf")])
    offer = incoming(tablet)
    tablet("POST", f"/api/incoming/{offer['id']}/accept")
    tablet_id = tablet.state()["me"]["id"]
    wait_for(lambda: target_states(phone, job) == {"Tablet": "accepted"})
    phone("POST", f"/api/send/{job}/targets/{tablet_id}/direct", {"state": "trying"})
    phone("POST", f"/api/send/{job}/targets/{tablet_id}/direct", {"state": "failed"})
    assert upload(phone, job, 0, b"pdf")[0] == 200
    assert tablet("GET", "/api/inbox/files/doc.pdf", raw=True) == (200, b"pdf")


def test_only_the_two_ends_can_exchange_connection_messages(make_node: NodeFactory) -> None:
    a = make_node("Alpha")
    phone, tablet, stranger = visitor(a, "Phone"), visitor(a, "Tablet"), visitor(a, "Stranger")
    job = send(phone, [device_id(phone, "Tablet")], [("a.txt", b"x")])
    offer = incoming(tablet)
    tablet_id = tablet.state()["me"]["id"]
    # Not accepted yet: nothing can be sent.
    assert (
        phone("POST", "/api/signal", {"job": job, "target": tablet_id, "kind": "offer", "sdp": ""})[
            0
        ]
        == 409
    )
    tablet("POST", f"/api/incoming/{offer['id']}/accept")
    # Someone else can't use the job or the session.
    assert (
        stranger(
            "POST", "/api/signal", {"job": job, "target": tablet_id, "kind": "offer", "sdp": ""}
        )[0]
        == 404
    )
    assert (
        stranger("POST", "/api/signal", {"session": offer["id"], "kind": "answer", "sdp": ""})[0]
        == 404
    )
    assert (
        stranger("POST", f"/api/incoming/{offer['id']}/direct", {"index": 0, "received": 1})[0]
        == 404
    )
    # Unknown kinds and huge messages are refused; forged app-to-app messages too.
    assert (
        phone("POST", "/api/signal", {"job": job, "target": tablet_id, "kind": "evil", "sdp": ""})[
            0
        ]
        == 400
    )
    assert (
        phone(
            "POST",
            "/api/signal",
            {"job": job, "target": tablet_id, "kind": "offer", "sdp": "x" * 70_000},
        )[0]
        == 400
    )
    forged = Client(a.local_url)(
        "POST",
        "/api/p2p/v1/signal",
        {
            "dir": "to_receiver",
            "session": offer["id"],
            "secret": "guess",
            "kind": "offer",
            "sdp": "",
        },
    )
    assert forged[0] == 404
    assert _signals(tablet) == []
