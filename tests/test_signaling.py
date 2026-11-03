"""WebRTC connection messages between apps can't be forged, changed or replayed.

A direct transfer goes from one browser to another over a WebRTC data channel
(DTLS). Each browser checks the other's DTLS certificate against the
fingerprint in the SDP it was handed, so whoever can change an SDP on its way
can sit in the middle. Between two apps the SDP travels over plain HTTP; for an
encrypted transfer each message carries a MAC under that transfer's key (the
pair key, or the one-off key of an unpaired transfer) over its transfer,
direction, counter and SDP. See docs/encryption.md, "Direct transfers".
"""

from __future__ import annotations

import hmac
from typing import Any

import pytest

from open_transfer import mesh as mesh_module
from open_transfer.node import Node
from tests.conftest import NodeFactory
from tests.test_encrypted_transfers import paired, target
from tests.test_mesh import (
    Client,
    _signals,
    connect,
    incoming,
    owner,
    send,
    target_states,
    visitor,
    wait_for,
)

SDP = (
    "v=0\r\no=- 46117 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
    "a=fingerprint:sha-256 4A:AD:B9:B1:3F:82:18:3B:54:02:12:DF:3E:5D:49:6B:"
    "19:E5:7C:AB:9F:2B:E7:7E:8E:0A:D8:16:2E:CB:83:0B\r\n"
    "a=candidate:1 1 udp 2122260223 192.168.1.20 54321 typ host\r\n"
)


class Wire:
    """Every connection message one app sends to another, as it crossed the network."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.hold = False  # record but don't deliver (someone in the middle took it)

    def last(self, direction: str) -> dict[str, Any]:
        return dict(next(b for b in reversed(self.bodies) if b.get("dir") == direction))


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Wire:
    wire = Wire()
    real = mesh_module.Mesh._send_signal

    def record(self: mesh_module.Mesh, peer: Any, body: dict[str, Any]) -> None:
        wire.bodies.append(dict(body))
        if not wire.hold:
            real(self, peer, body)

    monkeypatch.setattr(mesh_module.Mesh, "_send_signal", record)
    return wire


def apps(make_node: NodeFactory, mode: str) -> tuple[Node, Node]:
    if mode == "paired":
        return paired(make_node)
    a, b = make_node("Alpha"), make_node("Bravo")  # unpaired: a one-off key
    connect(a, b)
    return a, b


def accepted_job(a: Node, tabs: list[Client]) -> tuple[str, dict[str, str]]:
    """Alpha's owner sends to browsers on Bravo, which accept. (job, {tab id: session})."""
    alice = owner(a)
    ids = [t.state()["me"]["id"] for t in tabs]
    for tab_id in ids:
        wait_for(lambda tab_id=tab_id: tab_id in {d["id"] for d in alice.state()["devices"]})
    job = send(alice, ids, [("clip.mp4", b"v" * 100)])
    sessions = {}
    for tab_id, tab in zip(ids, tabs, strict=True):
        offer = incoming(tab)
        tab("POST", f"/api/incoming/{offer['id']}/accept")
        sessions[tab_id] = offer["id"]
    wait_for(lambda: set(target_states(alice, job).values()) == {"accepted"})
    for tab_id in ids:
        alice("POST", f"/api/send/{job}/targets/{tab_id}/direct", {"state": "trying"})
    return job, sessions


def offer(alice: Client, job: str, tab_id: str, sdp: str = SDP) -> int:
    body = {"job": job, "target": tab_id, "kind": "offer", "sdp": sdp}
    return int(alice("POST", "/api/signal", body)[0])


def answer(tab: Client, session: str, sdp: str = SDP) -> int:
    return int(tab("POST", "/api/signal", {"session": session, "kind": "answer", "sdp": sdp})[0])


def post(node: Node, body: dict[str, Any]) -> int:
    """Someone on the network posts a connection message straight to ``node``."""
    return int(Client(node.local_url)("POST", "/api/p2p/v1/signal", body)[0])


@pytest.fixture(params=["paired", "unpaired"])
def direct(
    request: pytest.FixtureRequest, make_node: NodeFactory, wire: Wire
) -> tuple[Node, Node, Client, str, str, str]:
    a, b = apps(make_node, request.param)
    tab = visitor(b, "Galaxy Tab", form="tablet")
    job, sessions = accepted_job(a, [tab])
    [(tab_id, session)] = sessions.items()
    assert target(a, job)["encrypted"] is True
    return a, b, tab, job, tab_id, session


# ---------------------------------------------------------------- it works


def test_messages_still_reach_the_other_browser(
    direct: tuple[Node, Node, Client, str, str, str], wire: Wire
) -> None:
    a, _, tab, job, tab_id, session = direct
    alice = owner(a)
    assert offer(alice, job, tab_id) == 200
    assert wait_for(lambda: _signals(tab)) == [{"kind": "offer", "sdp": SDP, "session": session}]
    assert answer(tab, session) == 200
    got = wait_for(lambda: _signals(alice))
    assert got == [{"kind": "answer", "sdp": SDP, "job": job, "target": tab_id}]
    sent = wire.last("to_receiver")
    assert len(sent["mac"]) == 64  # bound to this transfer…
    assert sent["n"] == 1  # …and counted
    assert wire.last("to_sender")["n"] == 1


# ------------------------------------------------------------ attacks on it


@pytest.mark.parametrize(
    ("what", "change"),
    [
        ("DTLS fingerprint", lambda b: {**b, "sdp": b["sdp"].replace("4A:AD", "66:66")}),
        ("ICE candidate", lambda b: {**b, "sdp": b["sdp"].replace("192.168.1.20", "10.6.6.6")}),
        ("whole SDP", lambda b: {**b, "sdp": "v=0\r\n"}),
        ("kind", lambda b: {**b, "kind": "bye"}),
        ("counter", lambda b: {**b, "n": b["n"] + 1}),
        ("no MAC (a downgrade)", lambda b: {k: v for k, v in b.items() if k not in {"mac", "n"}}),
        ("a MAC from another key", lambda b: {**b, "mac": hmac.new(b"other", b"x", "sha256").hexdigest()}),
    ],
)  # fmt: skip
def test_a_changed_message_is_refused(
    direct: tuple[Node, Node, Client, str, str, str], wire: Wire, what: str, change: Any
) -> None:
    a, b, tab, job, tab_id, session = direct
    wire.hold = True  # the real one hasn't arrived: only the MAC can tell
    assert offer(owner(a), job, tab_id) == 200
    genuine = wire.last("to_receiver")
    assert post(b, change(genuine)) == 403, what
    assert _signals(tab) == []
    assert post(b, genuine) == 200  # the forgery used nothing up
    assert _signals(tab) == [{"kind": "offer", "sdp": SDP, "session": session}]


def test_a_replayed_message_is_refused(
    direct: tuple[Node, Node, Client, str, str, str], wire: Wire
) -> None:
    a, b, tab, job, tab_id, _ = direct
    assert offer(owner(a), job, tab_id) == 200
    wait_for(lambda: _signals(tab))
    assert post(b, wire.last("to_receiver")) == 403  # would restart the browser's connection
    assert _signals(tab) == []


def test_a_message_moved_to_another_transfer_is_refused(make_node: NodeFactory, wire: Wire) -> None:
    a, b = paired(make_node)
    tab = visitor(b, "Galaxy Tab", form="tablet")
    first, s1 = accepted_job(a, [tab])
    _, s2 = accepted_job(a, [tab])
    [tab_id] = s1
    assert offer(owner(a), first, tab_id) == 200
    wait_for(lambda: _signals(tab))
    moved = wire.last("to_receiver")
    moved["session"] = s2[tab_id]
    moved["secret"] = b.mesh._incoming[s2[tab_id]].secret  # it crossed the network in the clear
    assert post(b, moved) == 403
    assert _signals(tab) == []


def test_an_answer_moved_to_another_receiver_in_a_group_is_refused(
    make_node: NodeFactory, wire: Wire
) -> None:
    a, b = paired(make_node)
    tab, phone = visitor(b, "Galaxy Tab", form="tablet"), visitor(b, "Pixel 8")
    job, sessions = accepted_job(a, [tab, phone])
    tab_id, phone_id = list(sessions)
    alice = owner(a)
    assert offer(alice, job, tab_id) == 200
    wait_for(lambda: _signals(tab))
    assert answer(tab, sessions[tab_id]) == 200
    wait_for(lambda: _signals(alice))
    moved = wire.last("to_sender")
    moved["target"] = phone_id
    moved["secret"] = a.mesh._jobs[job].deliveries[phone_id].remote_secret
    assert post(a, moved) == 403  # the tab can't answer for the phone
    assert _signals(alice) == []


def test_messages_for_a_finished_transfer_are_refused(
    direct: tuple[Node, Node, Client, str, str, str], wire: Wire
) -> None:
    a, b, tab, job, tab_id, session = direct
    b.mesh._incoming[session].state = mesh_module.CANCELED  # Alpha hasn't heard yet
    assert offer(owner(a), job, tab_id) == 409
    assert _signals(tab) == []


def test_a_lost_message_can_be_sent_again(
    direct: tuple[Node, Node, Client, str, str, str],
    wire: Wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _, tab, job, tab_id, session = direct
    real = a.mesh._http
    drops = [1]

    def flaky(peer: Any, method: str, path: str, **kw: Any) -> tuple[int, dict[str, Any]]:
        if path.endswith("/signal") and drops:
            drops.pop()
            raise OSError("Wi-Fi dropped")
        return real(peer, method, path, **kw)

    monkeypatch.setattr(a.mesh, "_http", flaky)
    assert offer(owner(a), job, tab_id) == 502  # the page gives up or tries again
    assert offer(owner(a), job, tab_id) == 200
    assert wait_for(lambda: _signals(tab)) == [{"kind": "offer", "sdp": SDP, "session": session}]
    assert wire.last("to_receiver")["n"] == 2  # a new message, not a replay


# ----------------------------------------------------------------- legacy


def test_an_older_app_still_swaps_messages_labelled_plain(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch, wire: Wire
) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    real_info = b.mesh.self_info  # from before Phase J: nothing to encrypt to
    monkeypatch.setattr(
        b.mesh, "self_info", lambda **kw: {k: v for k, v in real_info(**kw).items() if k != "kx"}
    )
    connect(a, b)
    tab = visitor(b, "Galaxy Tab", form="tablet")
    job, sessions = accepted_job(a, [tab])
    [(tab_id, session)] = sessions.items()
    assert target(a, job)["encrypted"] is False  # shown as not encrypted
    assert offer(owner(a), job, tab_id) == 200
    assert wait_for(lambda: _signals(tab)) == [{"kind": "offer", "sdp": SDP, "session": session}]
    assert "mac" not in wire.last("to_receiver")


def test_the_mac_covers_what_the_docs_say() -> None:
    from open_transfer import filecrypt

    key = filecrypt.signal_key(b"k" * 32, b"s" * 32, "A", "B", "T")
    fields = ["to_receiver", "S", "J", "T", "offer", SDP, 1]
    mac = filecrypt.signal_mac(key, fields)
    assert mac == filecrypt.signal_mac(key, list(fields))
    assert mac != filecrypt.signal_mac(key, [*fields[:-1], 2])
    other = filecrypt.signal_key(b"k" * 32, b"t" * 32, "A", "B", "T")  # another transfer
    assert mac != filecrypt.signal_mac(other, fields)
    assert key != b"k" * 32  # derived, never the pair key itself
