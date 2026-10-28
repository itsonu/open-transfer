"""Trusted Sync Network, protocol level: sealing, the hybrid clock, and the
engine's rules (fan-out, dedup, loops, stale and expired clips, the offline
queue, introductions, revocation, policies and relaying).

Engines talk through an in-memory network here; tests/test_sync_api.py runs
the same engine over real HTTP between app processes.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import pytest

from open_transfer.devices import TrustStore, new_device_id
from open_transfer.sync.clock import HybridClock
from open_transfer.sync.engine import SyncEngine
from open_transfer.sync.protocol import MAX_TEXT_BYTES, SyncError, classify
from open_transfer.sync.seal import SealError, hkdf, open_sealed, seal
from open_transfer.sync.store import SyncStore

# ------------------------------------------------------------------ sealing


def test_hkdf_matches_rfc_5869() -> None:
    # RFC 5869, test case 1.
    okm = hkdf(
        bytes.fromhex("0b" * 22),
        salt=bytes.fromhex("000102030405060708090a0b0c"),
        info=bytes.fromhex("f0f1f2f3f4f5f6f7f8f9"),
        length=42,
    )
    assert okm.hex() == (
        "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"
    )


def test_seal_round_trip_and_tamper_detection() -> None:
    key = bytes(range(32))
    header = {"kind": "event", "id": "e1", "dst": "d-0011"}
    sealed = seal(key, header, "Grüße 👋".encode())
    assert "Grüße".encode() not in json.dumps(sealed).encode()
    assert open_sealed(key, header, sealed) == "Grüße 👋".encode()
    assert seal(key, header, b"x")["nonce"] != seal(key, header, b"x")["nonce"]

    with pytest.raises(SealError):
        open_sealed(bytes(32), header, sealed)  # wrong key
    with pytest.raises(SealError):
        open_sealed(key, {**header, "dst": "d-0012"}, sealed)  # header changed
    flipped = dict(
        sealed, ct=sealed["ct"][:-4] + ("AAAA" if sealed["ct"][-4:] != "AAAA" else "BBBB")
    )
    with pytest.raises(SealError):
        open_sealed(key, header, flipped)
    with pytest.raises(SealError):
        open_sealed(key, header, {"nonce": "!", "ct": "", "tag": ""})


# -------------------------------------------------------------------- clock


def test_hybrid_clock_is_monotonic_and_follows_newer_clocks() -> None:
    now = [1000.0]
    clock = HybridClock(lambda: now[0])
    a = clock.tick()
    b = clock.tick()
    assert b > a  # same millisecond → the counter moves
    clock.observe((a[0] + 5000, 7))  # another device is 5 s ahead
    c = clock.tick()
    assert c > (a[0] + 5000, 7)
    now[0] = 2000.0
    assert clock.tick()[0] == 2_000_000


def test_hybrid_clock_refuses_clocks_from_the_far_future() -> None:
    clock = HybridClock(lambda: 1000.0)
    with pytest.raises(SyncError):
        clock.observe((1000_000 + 3600_000, 0))


# ------------------------------------------------------- in-memory network


class Clipboard:
    """What a native agent does: holds the OS clipboard and records remote writes."""

    def __init__(self) -> None:
        self.text = ""
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.refuse = False

    def write(self, text: str, info: dict[str, Any]) -> bool:
        if self.refuse:
            return False
        self.text = text
        self.writes.append((text, info))
        return True


def done(fn: Callable[[], Any]) -> Future[Any]:
    future: Future[Any] = Future()
    try:
        future.set_result(fn())
    except Exception as exc:  # pragma: no cover - surfaced by .result()
        future.set_exception(exc)
    return future


class Net:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.devices: dict[str, Device] = {}
        self.down: set[str] = set()
        self.cut: set[frozenset[str]] = set()  # pairs that can't reach each other directly
        self.wire: list[dict[str, Any]] = []
        self.errors: list[BaseException] = []  # anything but a refusal is a bug
        self.now = [time.time()]

    def add(self, name: str, *, platform: str = "macos") -> Device:
        device = Device(self, name, platform)
        self.devices[device.id] = device
        return device

    def post(self, sender: str, dst: str, kind: str, body: dict[str, Any]) -> tuple[int, dict]:
        if dst in self.down or sender in self.down or frozenset((sender, dst)) in self.cut:
            raise OSError("unreachable")
        self.wire.append(body)
        try:
            return 200, self.devices[dst].engine.handle(sender, kind, body)
        except SyncError as exc:
            return exc.status, {"error": {"code": exc.code}}
        except Exception as exc:
            self.errors.append(exc)
            raise

    def pair(self, a: Device, b: Device) -> None:
        import secrets

        key = secrets.token_bytes(32)
        a.trust.add(b.id, b.name, key)
        b.trust.add(a.id, a.name, key)
        a.engine.paired(b.id)
        b.engine.paired(a.id)

    def online(self) -> set[str]:
        return {d for d in self.devices if d not in self.down}


class Device:
    def __init__(self, net: Net, name: str, platform: str) -> None:
        self.net = net
        self.id = new_device_id()
        self.name = name
        self.platform = platform
        state = net.tmp / name
        self.trust = TrustStore(state)
        self.store = SyncStore(state)
        self.selected: set[str] = set()
        self.clip = Clipboard()
        self.engine = self.make_engine()

    def make_engine(self) -> SyncEngine:
        engine = SyncEngine(
            me=lambda: {
                "id": self.id,
                "name": self.name,
                "form": "computer",
                "platform": self.platform,
            },
            trust=self.trust,
            store=self.store,
            selected=lambda: set(self.selected),
            post=lambda dst, kind, body: self.net.post(self.id, dst, kind, body),
            online=lambda: self.net.online() - {self.id},
            describe=lambda i: (
                {"id": i, "name": d.name, "form": "computer", "platform": d.platform}
                if (d := self.net.devices.get(i))
                else None
            ),
            submit=done,
            clock=lambda: self.net.now[0],
        )
        engine.set_writer(self.clip.write)
        return engine

    def copy(self, text: str, **kw: Any) -> dict[str, Any]:
        self.clip.text = text
        return self.engine.copy(text, **kw)


@pytest.fixture
def net(tmp_path: Path) -> Iterator[Net]:
    net = Net(tmp_path)
    yield net
    assert not net.errors, net.errors


def network(net: Net, *names: str, **policy: Any) -> list[Device]:
    """Devices that all synced on with ``policy``, each paired with the first one."""
    devices = [net.add(n) for n in names]
    for d in devices:
        d.engine.set_policy(enabled=True, **policy)
    for d in devices[1:]:
        net.pair(devices[0], d)
    return devices


def texts(device: Device) -> list[str]:
    return [t for t, _ in device.clip.writes]


# ------------------------------------------------------------ fan-out


def test_a_copy_reaches_every_trusted_device(net: Net) -> None:
    mac, win, pixel = network(net, "Mac", "Windows", "Pixel")
    result = mac.copy("hello from the Mac")
    assert result["status"] == "sent"
    assert sorted(result["delivered"]) == sorted([win.id, pixel.id])
    assert texts(win) == texts(pixel) == ["hello from the Mac"]
    # Introduced by the Mac: Windows and the Pixel trust each other without pairing.
    assert win.trust.get(pixel.id)
    assert pixel.trust.get(win.id)
    assert win.trust.get(pixel.id).key == pixel.trust.get(win.id).key  # type: ignore[union-attr]
    win.copy("and back from Windows")
    assert texts(mac) == ["and back from Windows"]
    assert texts(pixel) == ["hello from the Mac", "and back from Windows"]


def test_payloads_are_encrypted_on_the_wire(net: Net) -> None:
    mac, _win = network(net, "Mac", "Windows")
    mac.copy("top secret plan")
    assert net.wire
    assert all("top secret plan" not in json.dumps(body) for body in net.wire)


def test_selected_devices_only(net: Net) -> None:
    mac, win, pixel = network(net, "Mac", "Windows", "Pixel", target="selected")
    mac.selected = {win.id}
    win.selected = {mac.id}
    pixel.selected = {mac.id}
    mac.copy("only for Windows")
    assert texts(win) == ["only for Windows"]
    assert texts(pixel) == []


def test_excluded_device_gets_nothing(net: Net) -> None:
    mac, win, pixel = network(net, "Mac", "Windows", "Pixel")
    mac.engine.set_device(pixel.id, excluded=True)
    mac.copy("not for the Pixel")
    assert texts(win) == ["not for the Pixel"]
    assert texts(pixel) == []


def test_receivers_refuse_devices_they_dont_sync_with(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows")
    win.engine.set_device(mac.id, excluded=True)
    result = mac.copy("hello?")
    assert result["refused"] == [win.id]
    assert texts(win) == []


def test_a_refusal_shows_in_recent_and_the_log(net: Net, caplog: pytest.LogCaptureFixture) -> None:
    """A device with sync off says no; "Sent to …" alone hid that it never got there."""
    mac, win = network(net, "Mac", "Windows")
    win.engine.set_policy(enabled=False)
    with caplog.at_level(logging.INFO, logger="open_transfer"):
        mac.copy("hello?")
    latest = mac.engine.status()["recent"][0]
    assert latest["devices"] == []
    assert latest["refused"] == ["Windows"]
    assert "1 refused" in caplog.text


# ------------------------------------------------- loops, echoes, dedup


def test_received_clips_are_never_sent_on(net: Net) -> None:
    mac, win, pixel = network(net, "Mac", "Windows", "Pixel")
    mac.copy("once")
    sent = len(net.wire)
    # The agent on Windows sees its own remote write as a clipboard change.
    assert win.copy("once")["status"] == "skipped"
    assert win.copy("once")["reason"] == "echo"
    assert len(net.wire) == sent
    assert texts(mac) == []
    assert texts(pixel) == ["once"]


def test_the_same_event_is_applied_once(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows")
    mac.copy("dup")
    body = net.wire[-1]
    assert win.engine.handle(mac.id, "sync", body)["reason"] == "duplicate"
    assert texts(win) == ["dup"]


def test_replayed_old_sequence_numbers_are_dropped(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows")
    mac.copy("first")
    first = net.wire[-1]
    mac.copy("second")
    # Replaying the first one (same id, already seen) or a lower seq is ignored.
    assert win.engine.handle(mac.id, "sync", first)["applied"] is False
    assert texts(win) == ["first", "second"]


# ------------------------------------------------- stale and conflicting


def test_an_older_clip_never_overwrites_a_newer_local_copy(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows")
    net.down.add(win.id)
    mac.copy("older, from the Mac")
    net.now[0] += 5
    win.copy("newer, copied on Windows")
    net.down.discard(win.id)
    mac.engine.peer_online(win.id)
    assert texts(win) == []  # the queued older clip lost
    assert texts(mac) == []  # Windows's copy was made while it couldn't reach anyone...
    win.engine.peer_online(mac.id)
    assert texts(mac) == ["newer, copied on Windows"]  # ...and is delivered now
    assert mac.clip.text == win.clip.text == "newer, copied on Windows"


def test_concurrent_copies_converge_on_the_newest(net: Net) -> None:
    mac, win, pixel = network(net, "Mac", "Windows", "Pixel")
    net.down.update({mac.id, win.id, pixel.id})
    mac.copy("mac")
    net.now[0] += 1
    win.copy("windows")
    net.now[0] += 1
    pixel.copy("pixel")
    net.down.clear()
    for d in (mac, win, pixel):
        for other in (mac, win, pixel):
            if other is not d:
                d.engine.peer_online(other.id)
    assert {mac.clip.text, win.clip.text, pixel.clip.text} == {"pixel"}


def test_ties_are_broken_by_device_id(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows")
    frozen = net.now[0]
    net.down.update({mac.id, win.id})
    mac.engine._clock = HybridClock(lambda: frozen)  # identical clocks
    win.engine._clock = HybridClock(lambda: frozen)
    mac.copy("from mac")
    win.copy("from windows")
    net.down.clear()
    mac.engine.peer_online(win.id)
    win.engine.peer_online(mac.id)
    assert mac.clip.text == win.clip.text
    winner = "from mac" if mac.id > win.id else "from windows"
    assert mac.clip.text == winner


def test_a_local_change_seen_late_still_wins(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows")
    mac.copy("from the Mac")
    net.now[0] += 1
    # Windows copied something the agent hasn't reported yet when a clip arrives.
    win.clip.text = "typed on Windows"
    win.engine.before_apply = lambda: win.engine.copy(win.clip.text)
    net.now[0] += 1
    mac.copy("from the Mac, again")  # created before Windows reports its own copy
    assert win.clip.text == "typed on Windows"


# --------------------------------------------------- expiry and queueing


def test_queued_clips_expire_instead_of_arriving_late(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows", expiry=60)
    net.down.add(win.id)
    result = mac.copy("stale soon")
    assert result["queued"] == [win.id]
    net.now[0] += 61
    net.down.discard(win.id)
    mac.engine.peer_online(win.id)
    assert texts(win) == []
    assert mac.engine.status()["pending"] == 0


def test_only_the_newest_clip_waits_for_an_offline_device(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows")
    net.down.add(win.id)
    for text in ("one", "two", "three"):
        mac.copy(text)
        net.now[0] += 1
    assert mac.engine.status()["pending"] == 1
    net.down.discard(win.id)
    mac.engine.peer_online(win.id)
    assert texts(win) == ["three"]


def test_failed_deliveries_are_retried(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows")
    net.down.add(win.id)
    mac.copy("retry me")
    net.down.discard(win.id)
    net.now[0] += 30
    mac.engine.tick()
    assert texts(win) == ["retry me"]


def test_expired_events_are_refused_on_arrival(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows", expiry=30)
    net.down.add(win.id)
    mac.copy("late")
    body = mac.engine._pending[win.id].body
    net.now[0] += 31
    net.down.discard(win.id)
    assert win.engine.handle(mac.id, "sync", body)["reason"] == "expired"


# -------------------------------------------------- trust and revocation


def test_adding_a_device_needs_one_pairing(net: Net) -> None:
    mac, win, pixel = network(net, "Mac", "Windows", "Pixel")
    ipad_like = net.add("Tab")
    ipad_like.engine.set_policy(enabled=True)
    net.pair(win, ipad_like)  # paired with any one member
    for d in (mac, pixel):
        assert d.trust.get(ipad_like.id)
        assert ipad_like.trust.get(d.id)
    ipad_like.copy("hello, network")
    assert texts(mac) == texts(win) == texts(pixel) == ["hello, network"]


def test_turning_sync_on_later_joins_the_network(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows")
    late = net.add("Late")
    net.pair(mac, late)  # sync is off on "Late": no introductions yet
    assert not win.trust.get(late.id)
    late.engine.set_policy(enabled=True)
    assert win.trust.get(late.id)
    assert late.trust.get(win.id)


def test_revoking_a_device_removes_it_everywhere(net: Net) -> None:
    mac, win, pixel = network(net, "Mac", "Windows", "Pixel")
    mac.engine.revoke(pixel.id)
    assert not mac.trust.get(pixel.id)
    assert not win.trust.get(pixel.id)  # told by the Mac
    mac.copy("after revocation")
    assert texts(pixel) == []
    # Whatever the revoked device still sends is refused.
    assert pixel.copy("still trying")["refused"] == sorted([mac.id, win.id])
    assert texts(mac) == []
    assert texts(win) == ["after revocation"]


def test_a_revoked_device_cant_be_reintroduced_by_a_stale_introduction(net: Net) -> None:
    mac, win, pixel = network(net, "Mac", "Windows", "Pixel")
    stale_at = net.now[0] - 10
    net.now[0] += 1
    win.engine.revoke(pixel.id)
    net.now[0] += 1
    intro = mac.engine._introduction(win.id, pixel.id, key=b"k" * 32, at=stale_at)
    assert win.engine.handle(mac.id, "sync", intro.body)["applied"] is False
    assert not win.trust.get(pixel.id)
    # Pairing again with the code clears the tombstone.
    net.pair(win, pixel)
    assert win.trust.get(pixel.id)


def test_introductions_can_be_turned_off(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows")
    win.engine.set_policy(accept_introductions=False)
    newcomer = net.add("New")
    newcomer.engine.set_policy(enabled=True)
    net.pair(mac, newcomer)
    assert not win.trust.get(newcomer.id)


def test_leaving_the_network(net: Net) -> None:
    mac, win, pixel = network(net, "Mac", "Windows", "Pixel")
    pixel.engine.leave()
    assert not pixel.trust.all()
    assert not mac.trust.get(pixel.id)
    assert not win.trust.get(pixel.id)


def test_unpaired_senders_and_wrong_recipients_are_refused(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows")
    stranger = net.add("Stranger")
    stranger.engine.set_policy(enabled=True)
    with pytest.raises(SyncError) as err:
        win.engine.handle(stranger.id, "sync", {"env": {"kind": "event", "by": stranger.id}})
    assert err.value.status == 403
    mac.copy("for windows")
    body = net.wire[-1]
    with pytest.raises(SyncError):
        mac.engine.handle(win.id, "sync", body)  # addressed to Windows, not the Mac


# ------------------------------------------------------------- policies


def test_nothing_happens_while_sync_is_off(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows")
    mac.engine.set_policy(enabled=False)
    assert mac.copy("nope")["reason"] == "off"
    assert texts(win) == []


def test_pausing_everything_or_one_device(net: Net) -> None:
    mac, win, pixel = network(net, "Mac", "Windows", "Pixel")
    mac.engine.pause(60)
    assert mac.copy("paused")["reason"] == "paused"
    net.now[0] += 61
    assert mac.copy("resumed by itself")["status"] == "sent"
    mac.engine.pause(None)  # until resumed
    net.now[0] += 10**6
    assert mac.copy("still paused")["reason"] == "paused"
    mac.engine.resume()
    mac.engine.set_device(win.id, paused_until=net.now[0] + 60)
    mac.copy("not to windows for now")
    assert texts(win)[-1] != "not to windows for now"
    assert texts(pixel)[-1] == "not to windows for now"


def test_temporary_session(net: Net) -> None:
    mac, _win = network(net, "Mac", "Windows")
    mac.engine.set_policy(enabled=False, session_minutes=60)
    assert mac.copy("during the session")["status"] == "sent"
    net.now[0] += 3601
    assert mac.copy("after it")["reason"] == "off"


def test_manual_mode_only_sends_when_asked(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows")
    mac.engine.set_policy(mode="manual")
    assert mac.copy("automatic")["reason"] == "manual"
    assert mac.copy("button", manual=True)["status"] == "sent"
    assert texts(win) == ["button"]


def test_content_types(net: Net) -> None:
    assert classify("https://example.com/a?b=c") == "url"
    assert classify("see https://example.com") == "text"
    mac, win = network(net, "Mac", "Windows", types=["url"])
    assert mac.copy("plain words")["reason"] == "type"
    mac.copy("https://example.com/")
    assert texts(win) == ["https://example.com/"]
    assert win.clip.writes[0][1]["type"] == "url"


def test_sensitive_text_is_not_synced_automatically(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows")
    assert mac.copy("password: hunter22")["reason"] == "sensitive"
    with pytest.raises(SyncError) as err:
        mac.copy("password: hunter22", manual=True)
    assert err.value.code == "sensitive"
    mac.copy("password: hunter22", manual=True, confirm=True)
    assert win.clip.writes[-1] == ("password: hunter22", win.clip.writes[-1][1])
    assert win.clip.writes[-1][1]["sensitive"] is True
    mac.engine.set_policy(sensitive="allow")
    net.now[0] += 1
    assert mac.copy("482913")["status"] == "sent"


def test_password_manager_markers_and_blocked_apps(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows")
    assert mac.copy("from 1Password", concealed=True)["reason"] == "concealed"
    mac.engine.set_policy(blocked_sources=["com.example.Vault"])
    assert mac.copy("from the vault", source="com.example.vault")["reason"] == "blocked"
    assert texts(win) == []


def test_size_limit(net: Net) -> None:
    mac, _win = network(net, "Mac", "Windows")
    assert mac.copy("x" * (MAX_TEXT_BYTES + 1))["reason"] == "too_large"
    assert mac.copy("")["reason"] == "empty"


def test_policy_validation(net: Net) -> None:
    (mac,) = network(net, "Mac")
    for bad in (
        {"target": "everyone"},
        {"mode": "sometimes"},
        {"types": ["video"]},
        {"expiry": 5},
        {"expiry": 999_999},
        {"sensitive": "maybe"},
        {"session_minutes": -1},
        {"blocked_sources": "x"},
    ):
        with pytest.raises(SyncError):
            mac.engine.set_policy(**bad)
    with pytest.raises(SyncError):
        mac.engine.set_device("d-0000000000000000", excluded=True)  # not paired


def test_policy_survives_a_restart(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows")
    mac.engine.set_policy(mode="manual", types=["text"], expiry=300)
    mac.engine.set_device(win.id, excluded=True)
    again = SyncStore(net.tmp / "Mac")
    assert again.policy()["mode"] == "manual"
    assert again.policy()["expiry"] == 300
    assert again.device(win.id)["excluded"] is True


# --------------------------------------------------------------- relaying


def test_a_member_relays_when_two_devices_cant_see_each_other(net: Net) -> None:
    mac, win, pixel = network(net, "Mac", "Windows", "Pixel")
    net.cut.add(frozenset((win.id, pixel.id)))
    win.copy("via the Mac")
    assert texts(pixel) == ["via the Mac"]
    # The relay passes the sealed clip on; it is neither applied nor readable there.
    assert texts(mac) == ["via the Mac"]  # the Mac got its own copy directly
    assert sum(1 for b in net.wire if "to" in b) == 1


def test_relays_dont_chain(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows")
    mac.copy("x")
    env = dict(net.wire[-1]["env"], via=[new_device_id()])
    with pytest.raises(SyncError):
        mac.engine.handle(win.id, "forward", {"to": win.id, "env": env})


# ------------------------------------------------------------- privacy


def test_clip_contents_never_reach_the_log(net: Net, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    mac, win = network(net, "Mac", "Windows")
    mac.copy("my diary entry")
    net.down.add(win.id)
    mac.copy("another entry")
    assert "diary" not in caplog.text
    assert "entry" not in caplog.text


def test_status_reports_metadata_only(net: Net) -> None:
    mac, win = network(net, "Mac", "Windows")
    mac.copy("private words")
    status = mac.engine.status()
    assert "private words" not in json.dumps(status)
    assert status["active"] is True
    assert status["recent"][0]["direction"] == "sent"
    assert status["recent"][0]["size"] == len(b"private words")
    (device,) = status["devices"]
    assert device["id"] == win.id
    assert device["syncing"] is True
    assert device["last_sent"]
