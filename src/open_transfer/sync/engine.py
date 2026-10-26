"""The sync engine: what happens to a copy, and to a clip that arrives.

It runs inside the app's process (never in a page), so a closed window
doesn't stop it. A native agent reports local copies with :meth:`copy` and
writes remote clips through the writer given to :meth:`set_writer`. The app
passes requests from other members to :meth:`handle`.

Rules (``docs/sync.md`` has the reasoning):

- One envelope per recipient, sealed with that pair's key. Only the newest
  clip waits for an offline device, and nothing is delivered after it expires.
- A received clip is applied only if it is newer, in hybrid-clock order, than
  what is on this clipboard now, and it is never sent on as a new copy.
- Duplicates (same id, or an old sequence number) are dropped.
- Clip contents never reach the log, the status or the disk.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from functools import partial
from typing import Any, cast

from open_transfer.devices import TrustStore, valid_id
from open_transfer.security import log_safe as _safe
from open_transfer.sensitive import looks_sensitive
from open_transfer.sync.clock import HybridClock
from open_transfer.sync.protocol import (
    CONTROL_EXPIRY,
    MAX_SKEW,
    MAX_TEXT_BYTES,
    VERSION,
    Header,
    SyncError,
    classify,
    parse_header,
    parse_via,
)
from open_transfer.sync.seal import SealError, open_sealed, seal
from open_transfer.sync.store import FOREVER, SyncStore

log = logging.getLogger("open_transfer.sync")

Post = Callable[[str, str, dict[str, Any]], tuple[int, dict[str, Any]]]
Writer = Callable[[str, dict[str, Any]], bool]
DELIVERY_TIMEOUT = 20.0


def _inline(fn: Callable[[], Any]) -> Future[Any]:
    future: Future[Any] = Future()
    try:
        future.set_result(fn())
    except Exception as exc:
        future.set_exception(exc)
    return future


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class Pending:
    """An envelope on its way to one device."""

    dst: str
    body: dict[str, Any]
    expires: float
    control: bool = False
    attempts: int = 0
    next_try: float = 0.0
    sent_at: float = field(default_factory=time.time)

    @property
    def id(self) -> str:
        return str(self.body["env"]["id"])

    @property
    def kind(self) -> str:
        return str(self.body["env"]["kind"])


class SyncEngine:
    def __init__(
        self,
        *,
        me: Callable[[], dict[str, Any]],
        trust: TrustStore,
        store: SyncStore,
        selected: Callable[[], set[str]],
        post: Post,
        online: Callable[[], set[str]],
        describe: Callable[[str], dict[str, Any] | None],
        submit: Callable[[Callable[[], Any]], Future[Any]] = _inline,
        clock: Callable[[], float] = time.time,
        meet: Callable[[str, str], object] | None = None,
    ) -> None:
        self._me = me
        self._trust = trust
        self._store = store
        self._selected = selected
        self._post = post
        self._online = online
        self._describe = describe
        self._meet = meet  # (device id, "host:port"): go and find a device we were introduced to
        self._submit = submit
        self._now = clock
        self._clock = HybridClock(clock)
        self._lock = threading.RLock()
        self._pending: dict[str, Pending] = {}  # newest clip per offline device
        self._seen: dict[str, float] = {}  # event id → when it expires
        self._last_seq: dict[str, int] = {}
        self._current: tuple[int, int, str] | None = None  # order of what's on the clipboard
        self._last_local = ""  # digests, never the text
        self._last_remote = ""
        self._recent: deque[dict[str, Any]] = deque(maxlen=30)
        self._stats: dict[str, dict[str, Any]] = {}
        self._backoff: dict[
            str, tuple[int, float]
        ] = {}  # control message id → (attempts, next try)
        self._writer: Writer | None = None
        self._was_active = self.active()
        #: Called before a clip is applied, so the agent can report a copy it hasn't yet.
        self.before_apply: Callable[[], object] | None = None

    @property
    def id(self) -> str:
        return str(self._me()["id"])

    def set_writer(self, writer: Writer | None) -> None:
        self._writer = writer

    @property
    def can_write(self) -> bool:
        return self._writer is not None

    # =========================================================== policy

    def policy(self) -> dict[str, Any]:
        return self._store.policy()

    def active(self) -> bool:
        policy = self._store.policy()
        return bool(policy["enabled"] or policy["session_until"] > self._now())

    def paused(self) -> bool:
        return bool(self._store.policy()["paused_until"] > self._now())

    def _syncs_with(self, device_id: str) -> bool:
        """Clips go to (and are accepted from) ``device_id``."""
        if not self._trust.get(device_id):
            return False
        device = self._store.device(device_id)
        if device["paused_until"] > self._now():
            return False
        if self._store.policy()["target"] == "selected":
            return device_id in self._selected()
        return not device["excluded"]

    def set_policy(self, *, session_minutes: int | None = None, **fields: Any) -> dict[str, Any]:
        if session_minutes is not None:
            if not (isinstance(session_minutes, int) and 0 <= session_minutes <= 24 * 60):
                raise SyncError(400, "bad_request", "A session lasts 0 to 1440 minutes.")
            fields["session_until"] = self._now() + session_minutes * 60 if session_minutes else 0.0
        policy = self._store.set_policy(**fields)
        self._check_activation()
        return policy

    def set_device(
        self, device_id: str, *, excluded: bool | None = None, paused_until: float | None = None
    ) -> dict[str, Any]:
        if not (valid_id(device_id) and self._trust.get(device_id)):
            raise SyncError(404, "not_paired", "That device isn’t one of your trusted devices.")
        return self._store.set_device(device_id, excluded=excluded, paused_until=paused_until)

    def pause(self, seconds: float | None) -> None:
        """Pause everything for ``seconds`` (``None``: until resumed)."""
        until = FOREVER if seconds is None else self._now() + max(0.0, float(seconds))
        self._store.set_policy(paused_until=until)

    def resume(self) -> None:
        self._store.set_policy(paused_until=0.0)
        self.flush()

    def _check_activation(self) -> None:
        active = self.active()
        if active and not self._was_active:
            self._announce()
        self._was_active = active

    # ====================================================== local copies

    def copy(
        self,
        text: str,
        *,
        source: str = "",
        concealed: bool = False,
        manual: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """This device's clipboard now holds ``text``: send it where the policy says."""
        if not isinstance(text, str) or not text:
            return _skipped("empty")
        size = len(text.encode())
        if size > MAX_TEXT_BYTES:
            return _skipped("too_large")
        digest = _digest(text)
        with self._lock:
            if not manual:
                if digest == self._last_remote:
                    return _skipped("echo")  # our own write of a clip that arrived
                if digest == self._last_local:
                    return _skipped("unchanged")
            # Whatever happens next, this is now the newest thing on our clipboard.
            stamp = self._clock.tick()
            self._current = (stamp[0], stamp[1], self.id)
            self._last_local = digest
            self._last_remote = ""
        policy = self._store.policy()
        kind = classify(text)
        sensitive = looks_sensitive(text) is not None
        reason = ""
        if not self.active():
            reason = "off"
        elif self.paused():
            reason = "paused"
        elif concealed and not manual:
            reason = "concealed"
        elif not manual and _blocked(source, policy["blocked_sources"]):
            reason = "blocked"
        elif policy["mode"] == "manual" and not manual:
            reason = "manual"
        elif kind not in policy["types"]:
            reason = "type"
        elif sensitive and not manual and policy["sensitive"] == "block":
            reason = "sensitive"
        if reason:
            return _skipped(reason)
        if (sensitive or concealed) and manual and not confirm:
            raise SyncError(409, "sensitive", "This looks private. Send it anyway?")
        targets = sorted(d.id for d in self._trust.all() if self._syncs_with(d.id))
        if not targets:
            return _skipped("no_devices")
        now = self._now()
        event = {
            "seq": self._store.next_seq(),
            "hlc": [stamp[0], stamp[1]],
            "type": kind,
            "created": now,
            "expires": now + policy["expiry"],
        }
        payload = json.dumps({"text": text, "sensitive": sensitive or concealed}).encode()
        event_id = secrets.token_hex(16)
        outcomes: dict[str, list[str]] = {"delivered": [], "queued": [], "refused": []}
        futures = []
        for dst in targets:
            pending = self._envelope(dst, "event", payload, event_id=event_id, **event)
            with self._lock:
                self._pending.pop(dst, None)  # only the newest waits
            futures.append((dst, self._submit(partial(self._deliver, pending))))
        for dst, future in futures:
            try:
                outcome = future.result(timeout=DELIVERY_TIMEOUT)
            except Exception:
                log.debug("sync delivery failed", exc_info=True)
                outcome = "queued"
            outcomes.setdefault(outcome, []).append(dst)
        self._note(
            {
                "id": event_id,
                "direction": "sent",
                "type": kind,
                "size": size,
                "devices": [self._name(d) for d in outcomes["delivered"]],
                "queued": len(outcomes["queued"]),
            }
        )
        log.info(
            "Clipboard %s (%d bytes) synced to %d device(s), %d waiting",
            kind,
            size,
            len(outcomes["delivered"]),
            len(outcomes["queued"]),
        )
        return {"status": "sent", "event": event_id, **{k: sorted(v) for k, v in outcomes.items()}}

    # ========================================================== delivery

    def _envelope(
        self,
        dst: str,
        kind: str,
        payload: bytes,
        *,
        event_id: str | None = None,
        src: str | None = None,
        seq: int = 0,
        hlc: list[int] | None = None,
        type: str = "",
        created: float | None = None,
        expires: float | None = None,
    ) -> Pending:
        trusted = self._trust.get(dst)
        if trusted is None:
            raise SyncError(404, "not_paired", "Not a trusted device.")
        now = self._now()
        stamp = hlc or [0, 0]
        header = Header(
            v=VERSION,
            kind=kind,
            id=event_id or secrets.token_hex(16),
            src=src or self.id,
            by=self.id,
            dst=dst,
            seq=seq,
            hlc=(stamp[0], stamp[1]),
            type=type,
            created=now if created is None else created,
            expires=(now + CONTROL_EXPIRY) if expires is None else expires,
        )
        head = header.to_dict()
        env = {**head, **seal(bytes.fromhex(trusted.key), head, payload), "via": []}
        return Pending(dst, {"env": env}, header.expires, control=kind != "event")

    def _deliver(self, pending: Pending) -> str:
        """Try once now. Returns ``delivered``, ``queued``, ``refused`` or ``expired``."""
        now = self._now()
        if now > pending.expires:
            self._drop(pending)
            return "expired"
        status, reply = self._send(pending.dst, "sync", pending.body)
        if status == 0 and not pending.control:
            status, reply = self._relay(pending)
        if status == 200:
            self._drop(pending)
            if pending.kind == "event":
                self._stat(pending.dst, last_sent=now, last_error=None)
            elif pending.kind == "join" and isinstance(reply.get("members"), list):
                self._joined(pending.dst, reply["members"])
            return "delivered"
        if 400 <= status < 500 and status not in (408, 429):
            self._drop(pending)
            error = reply.get("error")
            code = error.get("code") if isinstance(error, dict) else ""
            self._stat(pending.dst, last_error=str(code or "refused"))
            return "refused"
        self._stat(pending.dst, last_error="unreachable")
        with self._lock:
            if pending.control:  # it stays in the outbox; back off up to 5 minutes
                attempts = self._backoff.get(pending.id, (0, 0.0))[0] + 1
                self._backoff[pending.id] = (attempts, now + min(300, 2**attempts))
            else:
                pending.attempts += 1
                pending.next_try = now + min(30, 2**pending.attempts)
                current = self._pending.get(pending.dst)
                if current is None or current.body["env"]["hlc"] <= pending.body["env"]["hlc"]:
                    self._pending[pending.dst] = pending
        return "queued"

    def _stat(self, device_id: str, **values: Any) -> None:
        with self._lock:
            stats = self._stats.setdefault(device_id, {})
            for name, value in values.items():
                if value is None:
                    stats.pop(name, None)
                else:
                    stats[name] = value

    def _send(self, dst: str, kind: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            return self._post(dst, kind, body)
        except OSError:
            return 0, {}

    def _relay(self, pending: Pending) -> tuple[int, dict[str, Any]]:
        """``dst`` is out of reach: ask another member that can see it to pass the clip on."""
        for relay in sorted(self._online() - {pending.dst, self.id}):
            if not self._trust.get(relay):
                continue
            status, reply = self._send(relay, "forward", {"to": pending.dst, **pending.body})
            if status == 200:
                return status, reply
        return 0, {}

    def _drop(self, pending: Pending) -> None:
        with self._lock:
            if self._pending.get(pending.dst) is pending:
                del self._pending[pending.dst]
            self._backoff.pop(pending.id, None)
        if pending.control:
            self._store.unqueue(pending.id)

    def _queue_control(self, pending: Pending) -> None:
        self._store.queue({"dst": pending.dst, "env": pending.body["env"]})
        with self._lock:  # sent right now; housekeeping retries it later if that fails
            self._backoff[pending.id] = (0, self._now() + 5)
        self._submit(partial(self._deliver, pending))

    def _controls(self, dst: str | None = None) -> list[Pending]:
        out = []
        for message in self._store.outbox():
            if dst is None or message["dst"] == dst:
                env = message["env"]
                out.append(Pending(message["dst"], {"env": env}, float(env["expires"]), True))
        return out

    def peer_online(self, device_id: str) -> None:
        """``device_id`` just came back: deliver what waits for it."""
        with self._lock:
            pending = self._pending.get(device_id)
        waiting = [pending] if pending else []
        waiting += self._controls(device_id)
        for item in waiting:
            self._submit(partial(self._deliver, item))

    def flush(self) -> None:
        for device_id in {p.dst for p in self._pending.values()} | {
            m["dst"] for m in self._store.outbox()
        }:
            self.peer_online(device_id)

    def tick(self) -> None:
        """Housekeeping: retry what's due, drop what expired."""
        now = self._now()
        self._check_activation()
        with self._lock:
            for event_id, expires in list(self._seen.items()):
                if expires < now - MAX_SKEW:
                    del self._seen[event_id]
            for dst, pending in list(self._pending.items()):
                if pending.expires < now:
                    del self._pending[dst]
            due = [p for p in self._pending.values() if p.next_try <= now]
            backoff = dict(self._backoff)
        self._store.drop_expired(now)
        for control in self._controls():
            if backoff.get(control.id, (0, 0.0))[1] <= now:
                due.append(control)
        for pending in due:
            self._submit(partial(self._deliver, pending))

    # ============================================================ receiving

    def handle(self, sender: str, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        """A request from member ``sender`` (already authenticated by its signature)."""
        if not (valid_id(sender) and self._trust.get(sender)):
            raise SyncError(403, "not_paired", "Only trusted devices can sync.")
        if not isinstance(body, dict):
            raise SyncError(400, "bad_request", "Expected an envelope.")
        if kind == "forward":
            return self._forward(sender, body)
        if kind != "sync":
            raise SyncError(404, "not_found", "Unknown sync request.")
        header = parse_header(body.get("env"))
        env = cast("dict[str, Any]", body["env"])  # parse_header checked it
        via = parse_via(env)
        if header.dst != self.id:
            raise SyncError(400, "wrong_device", "That was meant for another device.")
        if header.by != sender and via != [sender]:
            raise SyncError(403, "not_paired", "That didn’t come from the device that sealed it.")
        sealer = self._trust.get(header.by)
        if sealer is None:
            raise SyncError(403, "not_paired", "Only trusted devices can sync.")
        try:
            payload = json.loads(open_sealed(bytes.fromhex(sealer.key), header.to_dict(), env))
        except (SealError, ValueError):
            raise SyncError(
                403, "bad_seal", "That couldn’t be opened with your shared key."
            ) from None
        if not isinstance(payload, dict):
            raise SyncError(400, "bad_request", "Bad payload.")
        if header.kind == "event":
            return self._on_event(header, payload)
        if header.kind == "revoke":
            return self._on_revoke(header, payload)
        if header.kind == "intro":
            return self._on_intro(header, payload)
        return self._on_join(header, payload)

    def _forward(self, sender: str, body: dict[str, Any]) -> dict[str, Any]:
        dst = body.get("to")
        header = parse_header(body.get("env"))
        env = cast("dict[str, Any]", body["env"])
        if parse_via(env) or header.kind != "event":
            raise SyncError(400, "bad_route", "Clips are relayed once, directly.")
        if header.by != sender or header.dst != dst or dst == self.id:
            raise SyncError(400, "bad_route", "Bad relay request.")
        if not (self.active() and self._trust.get(str(dst))):
            raise SyncError(403, "not_allowed", "This device doesn’t relay for you.")
        status, reply = self._send(str(dst), "sync", {"env": {**env, "via": [self.id]}})
        if status != 200:
            raise SyncError(502, "unreachable", "Couldn’t reach that device either.")
        return reply

    def _on_event(self, header: Header, payload: dict[str, Any]) -> dict[str, Any]:
        now = self._now()
        if header.created > now + MAX_SKEW:
            raise SyncError(400, "clock", "That device's clock is too far ahead.")
        if header.expires < now:
            return {"applied": False, "reason": "expired"}
        if not self.active():
            raise SyncError(403, "off", "Clipboard sync is off on this device.")
        if self.paused():
            raise SyncError(403, "paused", "Clipboard sync is paused on this device.")
        if not self._syncs_with(header.by):
            raise SyncError(403, "not_allowed", "This device doesn’t sync with you.")
        if header.type not in self._store.policy()["types"]:
            raise SyncError(415, "type", "This device doesn’t take that kind of clip.")
        text = payload.get("text")
        if not isinstance(text, str) or not text or len(text.encode()) > MAX_TEXT_BYTES:
            raise SyncError(400, "bad_request", "Bad clip.")
        with self._lock:
            if header.id in self._seen:
                return {"applied": False, "reason": "duplicate"}
            if header.seq <= self._last_seq.get(header.src, -1):
                return {"applied": False, "reason": "old"}
            self._seen[header.id] = header.expires
            self._last_seq[header.src] = header.seq
        self._clock.observe(header.hlc)
        if self.before_apply is not None:
            try:
                self.before_apply()
            except Exception:
                log.debug("before_apply failed", exc_info=True)
        with self._lock:
            if self._current is not None and header.order <= self._current:
                self._stats.setdefault(header.by, {})["last_received"] = now
                return {"applied": False, "reason": "stale"}
            writer = self._writer
        name = self._name(header.src)
        if writer is None:
            return {"applied": False, "reason": "no_clipboard"}
        info = {
            "id": header.id,
            "type": header.type,
            "sensitive": payload.get("sensitive") is True,
            "from": name,
        }
        try:
            ok = bool(writer(text, info))
        except Exception:
            log.warning("Couldn’t write the clipboard", exc_info=True)
            ok = False
        with self._lock:
            if ok:
                self._current = header.order
                self._last_remote = _digest(text)
                self._last_local = ""
            self._stats.setdefault(header.by, {})["last_received"] = now
        self._note(
            {
                "id": header.id,
                "direction": "received",
                "type": header.type,
                "size": len(text.encode()),
                "devices": [name],
                "applied": ok,
            }
        )
        log.info("Clipboard %s (%d bytes) from %s", header.type, len(text.encode()), _safe(name))
        return {"applied": ok, "reason": "" if ok else "write_failed"}

    # =========================================================== the network

    def paired(self, device_id: str) -> None:
        """We just paired with ``device_id`` using its code."""
        self._store.clear_revoked(device_id)
        if self.active():
            self._join(device_id)

    def _announce(self) -> None:
        """Sync was just turned on: ask every member to introduce us around."""
        for member in self._members():
            self._join(member)

    def _members(self, exclude: set[str] | None = None) -> list[str]:
        skip = (exclude or set()) | {self.id}
        return sorted(d.id for d in self._trust.all() if d.id not in skip)

    def _join(self, member: str) -> None:
        payload = json.dumps({"members": self._members({member})}).encode()
        self._queue_control(self._envelope(member, "join", payload))

    def _on_join(self, header: Header, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_accepting()
        theirs = _ids(payload.get("members"))
        for member in self._members({header.by, *theirs}):
            self._introduce(header.by, member)
        return {"applied": True, "members": self._members({header.by})}

    def _joined(self, member: str, theirs_raw: list[Any]) -> None:
        """``member`` answered our join with the devices it knows."""
        theirs = _ids(theirs_raw)
        for other in self._members({member, *theirs}):
            self._introduce(member, other)

    def _introduce(self, a: str, b: str) -> None:
        """Give ``a`` and ``b`` a fresh key of their own, through us."""
        if a == b or not (self._trust.get(a) and self._trust.get(b)):
            return
        key, at = secrets.token_bytes(32), self._now()
        self._queue_control(self._introduction(a, b, key=key, at=at))
        self._queue_control(self._introduction(b, a, key=key, at=at))

    def _introduction(self, dst: str, device_id: str, *, key: bytes, at: float) -> Pending:
        info = self._describe(device_id) or {}
        trusted = self._trust.get(device_id)
        device = {
            "id": device_id,
            "name": str(info.get("name") or (trusted.name if trusted else "")),
            "platform": str(info.get("platform", "")),
            "address": str(info.get("address", "")),
        }
        payload = json.dumps({"device": device, "key": key.hex(), "at": at}).encode()
        return self._envelope(dst, "intro", payload)

    def _on_intro(self, header: Header, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_accepting()
        device = payload.get("device")
        key_hex, at = payload.get("key"), payload.get("at")
        if not (
            isinstance(device, dict)
            and valid_id(device.get("id"))
            and str(device["id"]).startswith("d-")
            and isinstance(key_hex, str)
            and isinstance(at, (int, float))
            and not isinstance(at, bool)
        ):
            raise SyncError(400, "bad_request", "Bad introduction.")
        device_id = str(device["id"])
        try:
            key = bytes.fromhex(key_hex)
        except ValueError:
            raise SyncError(400, "bad_request", "Bad introduction.") from None
        if len(key) < 16 or device_id in (self.id, header.by):
            raise SyncError(400, "bad_request", "Bad introduction.")
        if at <= self._store.revoked_at(device_id):
            return {"applied": False, "reason": "revoked"}
        existing = self._trust.get(device_id)
        if existing and (existing.paired_at, existing.key) >= (float(at), key.hex()):
            return {"applied": False, "reason": "known"}
        self._trust.add(device_id, str(device.get("name") or ""), key, at=float(at))
        address = device.get("address")
        if self._meet is not None and isinstance(address, str) and 0 < len(address) <= 300:
            self._meet(device_id, address)
        log.info(
            "Introduced to %s by %s", _safe(str(device.get("name"))), _safe(self._name(header.by))
        )
        return {"applied": True}

    def revoke(self, device_id: str) -> None:
        """Remove ``device_id`` from this device and tell every other member to as well."""
        now = self._now()
        members = self._members({device_id})
        self._forget(device_id, now)
        payload = json.dumps({"device": device_id, "at": now}).encode()
        for member in members:
            self._queue_control(self._envelope(member, "revoke", payload))

    def leave(self) -> dict[str, list[str]]:
        """Remove this device from the network: every member forgets it."""
        now = self._now()
        told: list[str] = []
        missed: list[str] = []
        payload = json.dumps({"device": self.id, "at": now}).encode()
        for member in self._members():
            pending = self._envelope(member, "revoke", payload)
            status, _ = self._send(member, "sync", pending.body)
            (told if status == 200 else missed).append(member)
        for member in self._members():
            self._forget(member, now)
        return {"told": told, "missed": missed}

    def _forget(self, device_id: str, at: float) -> None:
        self._trust.remove(device_id)
        self._store.revoke(device_id, at)
        with self._lock:
            self._pending.pop(device_id, None)
            self._stats.pop(device_id, None)

    def _on_revoke(self, header: Header, payload: dict[str, Any]) -> dict[str, Any]:
        device_id, at = payload.get("device"), payload.get("at")
        if not (valid_id(device_id) and isinstance(at, (int, float)) and not isinstance(at, bool)):
            raise SyncError(400, "bad_request", "Bad revocation.")
        if device_id == self.id:
            return {"applied": False, "reason": "self"}
        self._forget(str(device_id), float(at))
        log.info("%s removed a device from the network", _safe(self._name(header.by)))
        return {"applied": True}

    def _require_accepting(self) -> None:
        if not (self.active() and self._store.policy()["accept_introductions"]):
            raise SyncError(403, "not_accepting", "This device isn’t taking introductions.")

    # ============================================================ status

    def _name(self, device_id: str) -> str:
        info = self._describe(device_id)
        if info and info.get("name"):
            return str(info["name"])
        trusted = self._trust.get(device_id)
        return trusted.name if trusted else "Unknown device"

    def _note(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._recent.appendleft({**entry, "at": self._now()})

    def status(self) -> dict[str, Any]:
        policy = self._store.policy()
        now = self._now()
        online = self._online()
        with self._lock:
            recent = [dict(r) for r in self._recent]
            pending = len(self._pending)
            stats = {k: dict(v) for k, v in self._stats.items()}
        devices = []
        for trusted in sorted(self._trust.all(), key=lambda d: self._name(d.id).lower()):
            device = self._store.device(trusted.id)
            info = self._describe(trusted.id) or {}
            devices.append(
                {
                    "id": trusted.id,
                    "name": self._name(trusted.id),
                    "platform": info.get("platform", ""),
                    "online": trusted.id in online,
                    "syncing": self._syncs_with(trusted.id),
                    "excluded": device["excluded"],
                    "paused_until": device["paused_until"] if device["paused_until"] > now else 0,
                    **stats.get(trusted.id, {}),
                }
            )
        return {
            "active": self.active(),
            "paused": self.paused(),
            "policy": policy,
            "clipboard_access": self.can_write,
            "pending": pending,
            "waiting_messages": len(self._store.outbox()),
            "recent": recent,
            "devices": devices,
        }


def _skipped(reason: str) -> dict[str, Any]:
    return {"status": "skipped", "reason": reason, "delivered": [], "queued": [], "refused": []}


def _blocked(source: str, blocked: list[str]) -> bool:
    source = source.strip().lower()
    return bool(source) and any(b.lower() == source or b.lower() in source for b in blocked)


def _ids(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(v) for v in value[:500] if valid_id(v)}
