"""The device mesh: nearby devices, visitors, pairing and targeted transfers.

Vocabulary
----------
* **app** — a device running Open Transfer (desktop app, Android app, CLI).
  Every app runs its own HTTP server, announces itself (``discovery.py``)
  and talks to other apps **directly**.
* **owner** — the person using an app on that device (its local window /
  ``127.0.0.1``). The owner accepts or declines incoming files.
* **visitor** — a browser on another device that opened an app's link (e.g.
  scanned its QR code). Visitors have no server of their own, so the app
  they're connected to sends and receives on their behalf.

Sending
-------
1. The sender's UI asks its own app to create a *job* for chosen targets
   (``create_job``). For each target the app makes an *offer*: directly to
   the target app over HTTP, or locally when the target is its own owner or
   one of its visitors.
2. Each receiver accepts or declines (paired devices are accepted
   automatically). Offers expire after two minutes.
3. The UI then uploads each file once to its app, which streams it to every
   accepting receiver at the same time (``job_upload``) — nothing is staged
   on the sender's disk.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import queue
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from open_transfer import __version__
from open_transfer.config import Config
from open_transfer.devices import (
    IdentityStore,
    SignatureChecker,
    TrustStore,
    clean_form,
    clean_name,
    clean_platform,
    code_hint,
    new_pair_code,
    new_visitor_id,
    pair_key,
    pair_proof,
    sign,
    valid_id,
)
from open_transfer.discovery import Discovery
from open_transfer.security import RateLimiter, log_safe
from open_transfer.storage import (
    FileInfo,
    IncompleteUpload,
    Readable,
    Storage,
    StorageError,
    file_kind,
    safe_filename,
)

log = logging.getLogger("open_transfer")

OFFER_TTL = 120.0  # seconds a receiver has to accept
PEER_STALE = 15.0  # an app we haven't heard from for this long is "not nearby"
PEER_FORGET = 120.0  # …and is dropped from the list (paired apps stay, greyed out)
VISITOR_STALE = 25.0  # browsers poll every 1.5 s, hidden tabs every 10 s
VISITOR_FORGET = 24 * 3600.0
HELLO_EVERY = 6.0
FINISHED_KEEP = 90.0  # finished transfers stay in the UI this long
PAIR_CODE_TTL = 600.0
STALL_TIMEOUT = 60.0  # a receiver that takes no data for this long is dropped
CHUNK = 256 * 1024
MAX_FILES = 1000
P2P = "/api/p2p/v1"

# Incoming session states
PENDING, ACCEPTED, RECEIVING, DONE = "pending", "accepted", "receiving", "done"
DECLINED, EXPIRED, CANCELED, FAILED = "declined", "expired", "canceled", "failed"
FINAL = {DONE, DECLINED, EXPIRED, CANCELED, FAILED}

Listener = Callable[[str, dict[str, Any]], None]


class MeshError(StorageError):
    """An error with an HTTP status, shown to the user as-is."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


# ===================================================================== models


@dataclass
class Viewer:
    """Who is looking at the UI: the device's owner, or a visiting browser."""

    kind: str  # "owner" | "visitor"
    id: str
    name: str
    form: str
    platform: str
    trusted: bool = False

    @property
    def is_owner(self) -> bool:
        return self.kind == "owner"


@dataclass
class Peer:
    """Another app on the network."""

    id: str
    name: str
    form: str
    platform: str
    host: str
    port: int
    version: str = ""
    rev: int = -1
    last_seen: float = 0.0
    manual: bool = False
    visitors: list[dict[str, str]] = field(default_factory=list)
    accepts: str = "everyone"

    @property
    def online(self) -> bool:
        return time.monotonic() - self.last_seen < PEER_STALE

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class Visitor:
    id: str
    name: str
    form: str
    platform: str
    address: str
    last_seen: float
    trusted: bool = False

    @property
    def online(self) -> bool:
        return time.monotonic() - self.last_seen < VISITOR_STALE


@dataclass
class IncomingFile:
    name: str
    size: int
    mime: str
    received: int = 0
    state: str = PENDING
    saved_name: str | None = None
    error: str = ""


@dataclass
class IncomingSession:
    id: str
    secret: str
    origin: dict[str, Any]
    target: str  # our device id (owner) or a visitor id
    files: list[IncomingFile]
    paired: bool
    created: float = field(default_factory=time.monotonic)
    state: str = PENDING
    reason: str = ""
    finished: float = 0.0
    local: bool = False  # offered by our own owner/visitor (no HTTP involved)
    source: str = ""  # address the offer came from (rate limiting)

    @property
    def total(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def received(self) -> int:
        return sum(f.received for f in self.files)


@dataclass
class Delivery:
    """One target of an outgoing job."""

    target: dict[str, Any]
    peer: Peer | None = None  # app to contact; None when the target is local
    session: IncomingSession | None = None  # local targets
    remote_id: str = ""
    remote_secret: str = ""
    state: str = "offering"
    reason: str = ""
    sent: int = 0
    files_done: set[int] = field(default_factory=set)
    canceled: bool = False

    def view_state(self) -> str:
        if self.canceled:
            return CANCELED
        if self.session is not None:
            mapping = {PENDING: "waiting", RECEIVING: "sending"}
            return mapping.get(self.session.state, self.session.state)
        return self.state


@dataclass
class Job:
    id: str
    owner: str  # viewer id that created it
    origin: dict[str, Any]
    files: list[dict[str, Any]]
    deliveries: dict[str, Delivery]
    created: float = field(default_factory=time.monotonic)
    canceled: bool = False
    finished: float = 0.0


# ====================================================================== mesh


class Mesh:
    def __init__(self, config: Config, storage: Storage) -> None:
        self.config = config
        self.storage = storage
        state = config.state_path
        state.mkdir(parents=True, exist_ok=True)
        self.identity_store = IdentityStore(
            state,
            name=config.device_name,
            form=config.device_form,
            platform=config.device_platform,
        )
        self.trust = TrustStore(state)
        self._inbox_root = state / "inbox"
        self._lock = threading.RLock()
        self._peers: dict[str, Peer] = {}
        self._visitors: dict[str, Visitor] = {}
        self._inboxes: dict[str, Storage] = {}
        self._incoming: dict[str, IncomingSession] = {}
        self._jobs: dict[str, Job] = {}
        self._listeners: list[Listener] = []
        self._signatures = SignatureChecker()
        self._pair_limiter = RateLimiter(attempts=5, window=60)
        self._pair_pending: dict[str, tuple[str, str, dict[str, Any], str, float]] = {}
        self._pair_failures = 0
        self._find_waiters: dict[str, list[tuple[threading.Event, list[Peer]]]] = {}
        self._rotate_code()
        self._rev = 0
        self.port = 0
        self.discovery: Discovery | None = None
        self._executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="ot-mesh")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- lifecycle

    @property
    def identity(self) -> Any:
        return self.identity_store.identity

    @property
    def id(self) -> str:
        return str(self.identity.id)

    def start(self, port: int) -> None:
        """Start announcing on the network and keeping the device list fresh."""
        self.port = port
        if self.config.discovery:
            self.discovery = Discovery(
                self._describe, self._on_packet, port=self.config.discovery_port
            )
            self.discovery.start()
        self._thread = threading.Thread(target=self._housekeeping, name="ot-mesh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.discovery:
            self.discovery.stop()
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if not job.finished:
                self.cancel_job(job.id, job.owner, quiet=True)
        self._executor.shutdown(wait=False, cancel_futures=True)

    def add_listener(self, listener: Listener) -> None:
        """Get told about ``"offer"`` and ``"received"`` events (desktop/Android notifications)."""
        self._listeners.append(listener)

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                listener(event, data)
            except Exception:
                log.debug("listener failed for %s", event, exc_info=True)

    # ---------------------------------------------------------------- wire

    def _describe(self) -> dict[str, Any]:
        """Our discovery datagram."""
        ident = self.identity
        return {
            "id": ident.id,
            "name": ident.name,
            "form": ident.form,
            "platform": ident.platform,
            "port": self.port,
            "rev": self._rev,
            "ver": __version__,
        }

    def self_info(self, *, include_visitors: bool = True) -> dict[str, Any]:
        """What ``GET /api/p2p/v1/info`` and hello responses return."""
        ident = self.identity
        info: dict[str, Any] = {
            "id": ident.id,
            "name": ident.name,
            "form": ident.form,
            "platform": ident.platform,
            "port": self.port,
            "version": __version__,
            "accepts": "paired" if self.config.paired_only else "everyone",
        }
        if include_visitors:
            with self._lock:
                info["visitors"] = [
                    {"id": v.id, "name": v.name, "form": v.form, "platform": v.platform}
                    for v in self._visitors.values()
                    if v.online
                ]
        return info

    def _bump(self) -> None:
        self._rev += 1
        if self.discovery and self.discovery.working:
            self._executor.submit(self.discovery.announce)

    # --------------------------------------------------------- HTTP client

    def _http(
        self,
        peer: Peer,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode() if body is not None else b""
        all_headers = {"Accept": "application/json", "User-Agent": f"open-transfer/{__version__}"}
        if body is not None:
            all_headers["Content-Type"] = "application/json"
        trusted = self.trust.get(peer.id)
        if trusted:
            all_headers["X-OT-From"] = self.id
            all_headers["X-OT-Auth"] = sign(bytes.fromhex(trusted.key), method, path, self.id, data)
        all_headers.update(headers or {})
        conn = http.client.HTTPConnection(peer.host, peer.port, timeout=timeout)
        try:
            conn.request(method, path, body=data or None, headers=all_headers)
            res = conn.getresponse()
            raw = res.read(2_000_000)
        finally:
            conn.close()
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
        return res.status, parsed if isinstance(parsed, dict) else {}

    def verify_request(
        self, sender: str | None, header: str | None, method: str, path: str, body: bytes
    ) -> bool:
        """True if a request really comes from the paired device ``sender``."""
        if not sender or not header:
            return False
        trusted = self.trust.get(sender)
        if trusted is None:
            return False
        return self._signatures.verify(
            bytes.fromhex(trusted.key), header, method, path, sender, body
        )

    # ------------------------------------------------------------- peers

    def _upsert_peer(self, info: dict[str, Any], host: str, *, manual: bool = False) -> Peer | None:
        peer_id = info.get("id")
        port = info.get("port")
        if not valid_id(peer_id) or peer_id == self.id or not str(peer_id).startswith("d-"):
            return None
        if not isinstance(port, int) or not 1 <= port <= 65535:
            return None
        assert isinstance(peer_id, str)  # noqa: S101 - narrowed by valid_id
        with self._lock:
            peer = self._peers.get(peer_id)
            is_new = peer is None or not peer.online
            if peer is None:
                peer = Peer(peer_id, "", "computer", "unknown", host, port)
                self._peers[peer_id] = peer
            peer.name = clean_name(info.get("name"))
            peer.form = clean_form(info.get("form"))
            peer.platform = clean_platform(info.get("platform"))
            peer.host, peer.port = host, port
            peer.version = str(info.get("version") or info.get("ver") or "")[:20]
            peer.accepts = "paired" if info.get("accepts") == "paired" else "everyone"
            peer.manual = peer.manual or manual
            peer.last_seen = time.monotonic()
            if isinstance(info.get("visitors"), list):
                peer.visitors = _clean_visitors(info["visitors"])
            if is_new:
                log.info("Found %s (%s) at %s", log_safe(peer.name), peer.platform, peer.address)
        self.trust.rename(peer_id, peer.name)
        return peer

    def _on_packet(self, packet: dict[str, Any], src: str) -> None:
        if packet.get("id") == self.id:
            return
        kind = packet["type"]
        if kind == "find":
            if packet.get("find") == code_hint(self.pair_code) and self.discovery:
                self.discovery.send({"type": "reply", **self._describe(), "found": packet["find"]})
            return
        if kind == "bye":
            with self._lock:
                peer = self._peers.get(packet["id"])
                if peer:
                    peer.last_seen = 0.0
                    peer.visitors = []
            return
        with self._lock:
            known = self._peers.get(packet["id"])
            fresh = known is None or not known.online
            changed = known is not None and known.rev != packet.get("rev")
        peer = self._upsert_peer(packet, src)
        if peer is None:
            return
        peer.rev = packet.get("rev", -1) if isinstance(packet.get("rev"), int) else -1
        found = packet.get("found")
        if isinstance(found, str):
            with self._lock:
                for event, results in self._find_waiters.get(found, []):
                    results.append(peer)
                    event.set()
        if fresh:
            # Answer straight away, and say hello over HTTP so they learn about
            # us even if multicast only works in one direction.
            if kind == "announce" and self.discovery:
                self.discovery.reply_soon()
            self._executor.submit(self._hello, peer)
        elif changed:
            self._executor.submit(self._hello, peer)

    def _hello(self, peer: Peer) -> bool:
        try:
            status, info = self._http(
                peer, "POST", f"{P2P}/hello", body=self.self_info(include_visitors=False), timeout=3
            )
        except OSError:
            return False
        if status != 200:
            return False
        self._upsert_peer(info, peer.host, manual=peer.manual)
        return True

    def handle_hello(self, info: dict[str, Any], host: str) -> dict[str, Any]:
        self._upsert_peer(info, host)
        return self.self_info()

    def connect(self, address: str) -> Peer:
        """Add an app by ``host:port`` (for networks where discovery is blocked)."""
        host, port = _parse_address(address)
        probe = Peer("d-00000000", "", "computer", "unknown", host, port)
        try:
            status, info = self._http(probe, "GET", f"{P2P}/info", timeout=4)
        except OSError as exc:
            raise MeshError(502, "unreachable", f"Couldn’t reach {host}:{port}.") from exc
        if status != 200:
            raise MeshError(502, "not_open_transfer", f"{host}:{port} isn’t an Open Transfer app.")
        if info.get("id") == self.id:
            raise MeshError(400, "is_self", "That’s this device.")
        peer = self._upsert_peer(info, host, manual=True)
        if peer is None:
            raise MeshError(502, "not_open_transfer", f"{host}:{port} isn’t an Open Transfer app.")
        self._executor.submit(self._hello, peer)
        return peer

    # ------------------------------------------------------------ visitors

    def touch_visitor(
        self, visitor_id: str, *, name: str, form: str, platform: str, address: str, trusted: bool
    ) -> Visitor:
        with self._lock:
            visitor = self._visitors.get(visitor_id)
            changed = visitor is None or not visitor.online or visitor.name != name
            if visitor is None:
                visitor = Visitor(visitor_id, name, form, platform, address, 0.0)
                self._visitors[visitor_id] = visitor
            visitor.name, visitor.form, visitor.platform = name, form, platform
            visitor.address = address
            visitor.trusted = trusted
            visitor.last_seen = time.monotonic()
        if changed:
            self._bump()
        return visitor

    def inbox(self, visitor_id: str) -> Storage:
        with self._lock:
            store = self._inboxes.get(visitor_id)
            if store is None:
                store = Storage(
                    self._inbox_root / visitor_id,
                    reserve_bytes=self.config.reserve_disk_bytes,
                    trash_ttl=0,
                )
                self._inboxes[visitor_id] = store
            return store

    def has_inbox(self, visitor_id: str) -> bool:
        return (self._inbox_root / visitor_id).is_dir()

    # ------------------------------------------------------------- pairing

    @property
    def pair_code(self) -> str:
        if time.monotonic() - self._pair_code_at > PAIR_CODE_TTL:
            self._rotate_code()
        return self._pair_code

    def _rotate_code(self) -> None:
        self._pair_code = new_pair_code()
        self._pair_code_at = time.monotonic()
        self._pair_failures = 0

    def pair_code_expires_in(self) -> int:
        return max(0, round(PAIR_CODE_TTL - (time.monotonic() - self._pair_code_at)))

    def check_visitor_code(self, code: str, client: str) -> bool | float:
        """``True`` if ``code`` is the current pairing code; seconds to wait if rate-limited."""
        wait = self._pair_limiter.attempt(f"visitor:{client}")
        if wait:
            return wait
        if secrets.compare_digest(str(code).strip(), self.pair_code):
            self._pair_limiter.reset(f"visitor:{client}")
            self._rotate_code()
            return True
        self._note_pair_failure()
        return False

    def _note_pair_failure(self) -> None:
        self._pair_failures += 1
        if self._pair_failures >= 10:  # someone is guessing: invalidate the code
            log.warning("Too many wrong pairing codes; showing a new one.")
            self._rotate_code()

    def pair_begin(self, info: dict[str, Any], nonce_b: str, client: str) -> dict[str, Any]:
        """Step 1 on the device showing the code."""
        if not valid_id(info.get("id")) or not _valid_nonce(nonce_b):
            raise MeshError(400, "bad_request", "Invalid pairing request.")
        nonce_a = secrets.token_hex(16)
        with self._lock:
            now = time.monotonic()
            for key, pending in list(self._pair_pending.items()):
                if now - pending[4] > 60:
                    del self._pair_pending[key]
            self._pair_pending[str(info["id"])] = (nonce_a, nonce_b, info, client, now)
        return {"nonce": nonce_a, "device": self.self_info(include_visitors=False)}

    def pair_confirm(self, device_id: str, proof: str, client: str) -> dict[str, Any]:
        """Step 2 on the device showing the code."""
        wait = self._pair_limiter.attempt(f"pair:{client}")
        if wait:
            raise MeshError(
                429, "too_many_attempts", "Too many pairing attempts. Try again shortly."
            )
        with self._lock:
            pending = self._pair_pending.pop(device_id, None)
        if pending is None or pending[3] != client:
            raise MeshError(400, "no_pairing", "Start pairing again.")
        nonce_a, nonce_b, info, _, _ = pending
        code = self.pair_code
        expected = pair_proof(code, "b", nonce_a, nonce_b, self.id, device_id)
        if not secrets.compare_digest(expected, str(proof)):
            self._note_pair_failure()
            raise MeshError(403, "wrong_code", "That code isn’t right.")
        self._pair_limiter.reset(f"pair:{client}")
        key = pair_key(code, nonce_a, nonce_b, self.id, device_id)
        self.trust.add(device_id, clean_name(info.get("name")), key)
        self._rotate_code()
        self._upsert_peer(info, client)
        log.info("Paired with %s", log_safe(clean_name(info.get("name"))))
        return {"proof": pair_proof(code, "a", nonce_a, nonce_b, self.id, device_id)}

    def pair_with(self, code: str, address: str | None = None) -> Peer:
        """Pair with the app showing ``code`` (found on the network, or at ``address``)."""
        code = "".join(ch for ch in str(code) if ch.isdigit())
        if len(code) != 6:
            raise MeshError(400, "bad_code", "Enter the 6-digit code shown on the other device.")
        peer = self.connect(address) if address else self._find_by_code(code)
        nonce_b = secrets.token_hex(16)
        try:
            status, data = self._http(
                peer,
                "POST",
                f"{P2P}/pair",
                body={"device": self.self_info(include_visitors=False), "nonce": nonce_b},
            )
            if status != 200 or not _valid_nonce(data.get("nonce")):
                raise MeshError(502, "pair_failed", _remote_message(data, "Pairing failed."))
            nonce_a = str(data["nonce"])
            proof_b = pair_proof(code, "b", nonce_a, nonce_b, peer.id, self.id)
            status, data = self._http(
                peer, "POST", f"{P2P}/pair/confirm", body={"id": self.id, "proof": proof_b}
            )
        except OSError as exc:
            raise MeshError(502, "unreachable", f"Lost connection to {peer.name}.") from exc
        if status == 403:
            raise MeshError(403, "wrong_code", "That code isn’t right.")
        if status != 200:
            raise MeshError(status, "pair_failed", _remote_message(data, "Pairing failed."))
        expected = pair_proof(code, "a", nonce_a, nonce_b, peer.id, self.id)
        if not secrets.compare_digest(expected, str(data.get("proof", ""))):
            raise MeshError(502, "pair_failed", "The other device couldn’t prove it has the code.")
        self.trust.add(peer.id, peer.name, pair_key(code, nonce_a, nonce_b, peer.id, self.id))
        log.info("Paired with %s", log_safe(peer.name))
        return peer

    def _find_by_code(self, code: str) -> Peer:
        if not self.discovery or not self.discovery.working:
            raise MeshError(
                400, "no_discovery", "Discovery is off — enter the other device’s address too."
            )
        hint = code_hint(code)
        event = threading.Event()
        results: list[Peer] = []
        with self._lock:
            self._find_waiters.setdefault(hint, []).append((event, results))
        try:
            for _ in range(6):
                self.discovery.find(hint)
                if event.wait(0.6):
                    return results[0]
        finally:
            with self._lock:
                waiters = self._find_waiters.get(hint, [])
                waiters[:] = [w for w in waiters if w[0] is not event]
                if not waiters:
                    self._find_waiters.pop(hint, None)
        raise MeshError(
            404,
            "code_not_found",
            "No device on this network is showing that code. Check it, or enter its address.",
        )

    def unpair(self, device_id: str) -> bool:
        return self.trust.remove(device_id)

    # ----------------------------------------------------------- incoming

    def create_offer(
        self,
        *,
        origin: dict[str, Any],
        target: str,
        files: list[dict[str, Any]],
        paired: bool,
        local: bool = False,
        source: str = "",
    ) -> IncomingSession:
        """Someone wants to send ``files`` to ``target`` (our owner or a visitor)."""
        clean = _clean_files(files)
        owner_target = target == self.id
        if not owner_target:
            with self._lock:
                visitor = self._visitors.get(target)
            if visitor is None or not visitor.online:
                raise MeshError(404, "not_here", "That device isn’t connected anymore.")
        if owner_target and self.config.paired_only and not paired:
            raise MeshError(
                403, "paired_only", f"{self.identity.name} only accepts files from paired devices."
            )
        session = IncomingSession(
            id=secrets.token_urlsafe(9),
            secret=secrets.token_urlsafe(18),
            origin=origin,
            target=target,
            files=clean,
            paired=paired,
            local=local,
            source=source,
        )
        problem = self._space_problem(session)
        if problem:
            session.state, session.reason, session.finished = DECLINED, problem, time.monotonic()
        elif owner_target and (self.config.auto_accept or paired):
            self._accept(session)
        with self._lock:
            self._incoming[session.id] = session
        if session.state == PENDING:
            log.info(
                "%s wants to send %s file(s) (%s bytes)",
                log_safe(origin.get("name")),
                len(clean),
                session.total,
            )
            self._emit("offer", self._incoming_view(session))
        return session

    def pending_from(self, source: str) -> int:
        with self._lock:
            return sum(
                1 for s in self._incoming.values() if s.state == PENDING and s.source == source
            )

    def _space_problem(self, session: IncomingSession) -> str:
        limit = self.config.max_upload_size
        if limit and any(f.size > limit for f in session.files):
            return "A file is larger than this device accepts."
        store = self.storage if session.target == self.id else self.inbox(session.target)
        if session.total > store.usage()["free"]:
            return "Not enough free space on the receiving device."
        return ""

    def _accept(self, session: IncomingSession) -> None:
        session.state = ACCEPTED
        for f in session.files:
            f.state = "waiting"

    def decide(self, session_id: str, viewer: Viewer, accept: bool) -> IncomingSession:
        session = self._session_for(session_id, viewer)
        with self._lock:
            if session.state != PENDING:
                raise MeshError(409, "already_decided", "This transfer was already answered.")
            if accept:
                problem = self._space_problem(session)
                if problem:
                    raise MeshError(507, "insufficient_storage", problem)
                self._accept(session)
            else:
                session.state, session.finished = DECLINED, time.monotonic()
                session.reason = "Declined"
        return session

    def cancel_incoming(self, session_id: str, viewer: Viewer | None, *, secret: str = "") -> None:
        """Stop receiving: by the receiver (``viewer``) or the sender (``secret``)."""
        if viewer is not None:
            session = self._session_for(session_id, viewer)
        else:
            session = self._session_by_secret(session_id, secret)
        with self._lock:
            if session.state in FINAL:
                return
            session.state, session.finished = CANCELED, time.monotonic()
            session.reason = "Canceled by the sender" if viewer is None else "Canceled"

    def offer_status(self, session_id: str, secret: str) -> dict[str, Any]:
        session = self._session_by_secret(session_id, secret)
        return {
            "id": session.id,
            "state": session.state,
            "reason": session.reason,
            "files": [{"state": f.state, "received": f.received} for f in session.files],
        }

    def _session_for(self, session_id: str, viewer: Viewer) -> IncomingSession:
        with self._lock:
            session = self._incoming.get(session_id)
        if session is None or session.target != viewer.id:
            raise MeshError(404, "not_found", "That transfer is no longer available.")
        return session

    def _session_by_secret(self, session_id: str, secret: str) -> IncomingSession:
        with self._lock:
            session = self._incoming.get(session_id)
        if session is None or not secrets.compare_digest(session.secret, secret or ""):
            raise MeshError(404, "not_found", "Unknown transfer.")
        return session

    def receive_file(
        self,
        session_id: str,
        index: int,
        stream: Readable,
        length: int | None,
        *,
        secret: str | None = None,
        session: IncomingSession | None = None,
    ) -> FileInfo:
        """Save one offered file. Called for remote uploads and local deliveries."""
        if session is None:
            session = self._session_by_secret(session_id, secret or "")
        with self._lock:
            if session.state in {DECLINED, EXPIRED, CANCELED}:
                raise MeshError(410, session.state, session.reason or "This transfer was stopped.")
            if session.state not in {ACCEPTED, RECEIVING}:
                raise MeshError(409, "not_accepted", "The receiver hasn’t accepted yet.")
            if not 0 <= index < len(session.files):
                raise MeshError(404, "not_found", "No such file in this transfer.")
            item = session.files[index]
            if item.state in {RECEIVING, DONE}:
                raise MeshError(409, "duplicate", "This file is already being received.")
            if length is None:
                raise MeshError(411, "length_required", "Send a Content-Length header.")
            if length != item.size:
                raise MeshError(400, "size_mismatch", "The file size doesn’t match the offer.")
            item.state, item.received, item.error = RECEIVING, 0, ""
            session.state = RECEIVING
        store = self.storage if session.target == self.id else self.inbox(session.target)

        def count(n: int) -> None:
            item.received += n

        reader = _Watched(stream, count, lambda: session.state == CANCELED)
        try:
            saved = store.save_stream(
                item.name, reader, length=length, max_size=self.config.max_upload_size
            )
        except (StorageError, OSError, ValueError) as exc:
            with self._lock:
                item.state = FAILED
                item.error = str(exc) if isinstance(exc, StorageError) else "Interrupted"
                self._maybe_finish(session)
            if session.state == CANCELED:
                raise MeshError(410, CANCELED, session.reason) from exc
            if isinstance(exc, StorageError):
                raise
            raise IncompleteUpload("The transfer was interrupted before it finished.") from exc
        with self._lock:
            item.state, item.saved_name, item.received = DONE, saved.name, item.size
            self._maybe_finish(session)
        log.info(
            "Received %s (%s bytes) from %s", log_safe(saved.name), saved.size,
            log_safe(session.origin.get("name")),
        )  # fmt: skip
        if session.target == self.id:
            self._emit("received", {"path": str(store.root / saved.name), "name": saved.name})
        return saved

    def _maybe_finish(self, session: IncomingSession) -> None:
        if session.state == CANCELED:
            return
        if all(f.state in {DONE, FAILED} for f in session.files):
            failed = sum(f.state == FAILED for f in session.files)
            session.state = FAILED if failed == len(session.files) else DONE
            session.reason = f"{failed} file(s) failed" if failed else ""
            session.finished = time.monotonic()

    # ----------------------------------------------------------- outgoing

    def create_job(self, viewer: Viewer, targets: list[str], files: list[dict[str, Any]]) -> Job:
        clean = _clean_files(files)
        targets = list(dict.fromkeys(t for t in targets if isinstance(t, str)))
        if not targets:
            raise MeshError(400, "no_targets", "Choose at least one device.")
        if len(targets) > 64:
            raise MeshError(400, "too_many_targets", "Choose 64 devices or fewer.")
        origin = self._origin(viewer)
        deliveries: dict[str, Delivery] = {}
        for target_id in targets:
            deliveries[target_id] = self._resolve_target(target_id, viewer)
        job = Job(
            id=secrets.token_urlsafe(9),
            owner=viewer.id,
            origin=origin,
            files=[{"name": f.name, "size": f.size, "mime": f.mime} for f in clean],
            deliveries=deliveries,
        )
        with self._lock:
            self._jobs[job.id] = job
        for target_id, delivery in deliveries.items():
            if delivery.state != "offering":
                continue
            if delivery.peer is None:
                self._offer_local(job, target_id, delivery, viewer)
            else:
                self._executor.submit(self._offer_remote, job, target_id, delivery)
        return job

    def _origin(self, viewer: Viewer) -> dict[str, Any]:
        origin = {
            "id": viewer.id,
            "name": viewer.name,
            "form": viewer.form,
            "platform": viewer.platform,
        }
        if not viewer.is_owner:
            origin["via"] = self.id
            origin["via_name"] = self.identity.name
        return origin

    def _resolve_target(self, target_id: str, viewer: Viewer) -> Delivery:
        if target_id == viewer.id:
            raise MeshError(400, "is_self", "You can’t send files to yourself.")
        with self._lock:
            if target_id == self.id:
                return Delivery(target=self._target_view_self())
            visitor = self._visitors.get(target_id)
            if visitor is not None and visitor.online:
                return Delivery(
                    target=_target(
                        visitor.id, visitor.name, visitor.form, visitor.platform, "browser"
                    )
                )
            peer = self._peers.get(target_id)
            if peer is not None:
                delivery = Delivery(
                    target=_target(peer.id, peer.name, peer.form, peer.platform, "app"), peer=peer
                )
                if not peer.online:
                    delivery.state, delivery.reason = FAILED, f"{peer.name} isn’t nearby."
                return delivery
            for host in self._peers.values():
                for v in host.visitors:
                    if v["id"] == target_id and host.online:
                        target = _target(v["id"], v["name"], v["form"], v["platform"], "browser")
                        target["via_name"] = host.name
                        return Delivery(target=target, peer=host)
        raise MeshError(404, "unknown_device", "That device isn’t nearby anymore.")

    def _target_view_self(self) -> dict[str, Any]:
        ident = self.identity
        return _target(ident.id, ident.name, ident.form, ident.platform, "app")

    def _offer_local(self, job: Job, target_id: str, delivery: Delivery, viewer: Viewer) -> None:
        try:
            delivery.session = self.create_offer(
                origin=job.origin,
                target=target_id,
                files=job.files,
                paired=viewer.trusted and target_id == self.id,
                local=True,
            )
        except MeshError as exc:
            delivery.state, delivery.reason = FAILED, str(exc)

    def _offer_remote(self, job: Job, target_id: str, delivery: Delivery) -> None:
        peer = delivery.peer
        assert peer is not None  # noqa: S101 - remote deliveries always have a peer
        body = {
            "from": self.self_info(include_visitors=False),
            "origin": job.origin,
            "to": target_id,
            "files": job.files,
        }
        try:
            status, data = self._http(peer, "POST", f"{P2P}/offers", body=body, timeout=8)
        except OSError:
            delivery.state, delivery.reason = FAILED, f"Couldn’t reach {peer.name}."
            return
        if status != 201:
            delivery.state = DECLINED if status in {403, 507} else FAILED
            delivery.reason = _remote_message(data, f"{peer.name} refused the transfer.")
            return
        delivery.remote_id = str(data.get("id", ""))
        delivery.remote_secret = str(data.get("secret", ""))
        if job.canceled or delivery.canceled:
            # Canceled while the offer was on its way: withdraw it now we know its id.
            delivery.state = CANCELED
            with contextlib.suppress(OSError):
                self._http(
                    peer,
                    "DELETE",
                    f"{P2P}/offers/{delivery.remote_id}",
                    headers={"X-OT-Secret": delivery.remote_secret},
                )
            return
        self._follow_remote(job, delivery, data)

    def _follow_remote(self, job: Job, delivery: Delivery, data: dict[str, Any]) -> None:
        peer = delivery.peer
        assert peer is not None  # noqa: S101
        deadline = time.monotonic() + OFFER_TTL + 15
        errors = 0
        while True:
            state = data.get("state")
            if state == PENDING:
                delivery.state = "waiting"
            elif state in {ACCEPTED, RECEIVING}:
                if delivery.state in {"offering", "waiting"}:
                    delivery.state = ACCEPTED
                return
            elif state in FINAL:
                if delivery.state not in {"sending", DONE}:
                    delivery.state = state
                    delivery.reason = str(data.get("reason") or "")
                return
            if job.canceled or delivery.canceled or self._stop.is_set():
                return
            if time.monotonic() > deadline:
                delivery.state, delivery.reason = EXPIRED, "No answer"
                return
            time.sleep(0.8)
            try:
                status, data = self._http(
                    peer,
                    "GET",
                    f"{P2P}/offers/{delivery.remote_id}",
                    headers={"X-OT-Secret": delivery.remote_secret},
                    timeout=5,
                )
                if status != 200:
                    data = {"state": FAILED, "reason": _remote_message(data, "Transfer lost")}
                errors = 0
            except OSError:
                errors += 1
                if errors >= 5:
                    delivery.state, delivery.reason = FAILED, f"Lost connection to {peer.name}."
                    return
                data = {"state": delivery.state if delivery.state != "offering" else PENDING}

    def job_for(self, job_id: str, viewer: Viewer) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.owner != viewer.id:
            raise MeshError(404, "not_found", "That transfer is no longer available.")
        return job

    def job_upload(
        self, job_id: str, viewer: Viewer, index: int, stream: Readable, length: int | None
    ) -> dict[str, Any]:
        """Stream one file from the sender's browser to every accepting receiver at once."""
        job = self.job_for(job_id, viewer)
        if job.canceled:
            raise MeshError(410, CANCELED, "This transfer was canceled.")
        if not 0 <= index < len(job.files):
            raise MeshError(404, "not_found", "No such file in this transfer.")
        size = int(job.files[index]["size"])
        if length is None:
            raise MeshError(411, "length_required", "Send a Content-Length header.")
        if length != size:
            raise MeshError(400, "size_mismatch", "The file size doesn’t match.")
        sinks = [
            _Sink(self, job, target_id, delivery, index, size)
            for target_id, delivery in job.deliveries.items()
            if delivery.view_state() in {ACCEPTED, "sending"} and index not in delivery.files_done
        ]
        if not sinks:
            if any(d.view_state() in {"offering", "waiting"} for d in job.deliveries.values()):
                raise MeshError(409, "waiting", "Still waiting for the receivers to accept.")
            raise MeshError(409, "no_receivers", "Nobody accepted this transfer.")
        for sink in sinks:
            sink.start()
        received = 0
        try:
            while True:
                chunk = stream.read(CHUNK)
                if not chunk:
                    break
                received += len(chunk)
                live = [s for s in sinks if s.alive]
                if not live or job.canceled:
                    break
                for sink in live:
                    sink.feed(chunk)
        except Exception:
            for sink in sinks:
                sink.abort("The sender stopped the transfer.")
            raise
        if received != length or job.canceled:
            for sink in sinks:
                sink.abort("Canceled" if job.canceled else "The sender’s connection was lost.")
            for sink in sinks:
                sink.join(10)
            if job.canceled:
                raise MeshError(410, CANCELED, "This transfer was canceled.")
            if not any(s.alive for s in sinks) and received < length:
                return self._upload_result(job, index, sinks)
            raise IncompleteUpload("The upload was interrupted before it finished.")
        for sink in sinks:
            sink.finish()
        for sink in sinks:
            sink.join(STALL_TIMEOUT * 2)
        return self._upload_result(job, index, sinks)

    def _upload_result(self, job: Job, index: int, sinks: list[_Sink]) -> dict[str, Any]:
        results = {}
        for sink in sinks:
            ok = sink.ok
            delivery = sink.delivery
            if ok:
                delivery.files_done.add(index)
                if delivery.session is None and len(delivery.files_done) == len(job.files):
                    delivery.state = DONE
            elif delivery.session is None and delivery.state not in {CANCELED, DECLINED}:
                delivery.state, delivery.reason = FAILED, sink.error or "Transfer failed"
            results[sink.target_id] = {"ok": ok, "error": sink.error}
        if all(d.view_state() in FINAL for d in job.deliveries.values()):
            job.finished = time.monotonic()
        return {"targets": results}

    def cancel_job(self, job_id: str, viewer_id: str, *, quiet: bool = False) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.owner != viewer_id:
            if quiet:
                return
            raise MeshError(404, "not_found", "That transfer is no longer available.")
        job.canceled = True
        job.finished = job.finished or time.monotonic()
        for delivery in job.deliveries.values():
            self._cancel_delivery(delivery)

    def cancel_target(self, job_id: str, viewer: Viewer, target_id: str) -> None:
        job = self.job_for(job_id, viewer)
        delivery = job.deliveries.get(target_id)
        if delivery is None:
            raise MeshError(404, "not_found", "That device isn’t part of this transfer.")
        self._cancel_delivery(delivery)
        if all(d.view_state() in FINAL for d in job.deliveries.values()):
            job.finished = time.monotonic()

    def _cancel_delivery(self, delivery: Delivery) -> None:
        if delivery.view_state() in {DONE, DECLINED, EXPIRED, FAILED, CANCELED}:
            return
        delivery.canceled = True
        delivery.state = CANCELED
        if delivery.session is not None:
            with contextlib.suppress(MeshError):
                self.cancel_incoming(delivery.session.id, None, secret=delivery.session.secret)
        elif delivery.peer is not None and delivery.remote_id:
            peer, remote_id, secret = delivery.peer, delivery.remote_id, delivery.remote_secret

            def tell() -> None:
                with contextlib.suppress(OSError):
                    self._http(
                        peer, "DELETE", f"{P2P}/offers/{remote_id}", headers={"X-OT-Secret": secret}
                    )

            self._executor.submit(tell)

    # -------------------------------------------------------- housekeeping

    def _housekeeping(self) -> None:
        last_hello = 0.0
        manual_known: dict[str, float] = {}
        while not self._stop.wait(2.0):
            now = time.monotonic()
            with self._lock:
                for session in self._incoming.values():
                    if session.state == PENDING and now - session.created > OFFER_TTL:
                        session.state, session.reason, session.finished = EXPIRED, "No answer", now
                for key in [
                    k for k, s in self._incoming.items()
                    if s.state in FINAL and now - s.finished > FINISHED_KEEP
                ]:  # fmt: skip
                    del self._incoming[key]
                for key in [
                    k
                    for k, j in self._jobs.items()
                    if j.finished and now - j.finished > FINISHED_KEEP
                ]:
                    del self._jobs[key]
                for key in [
                    k for k, p in self._peers.items()
                    if now - p.last_seen > PEER_FORGET and not self.trust.get(k) and not p.manual
                ]:  # fmt: skip
                    del self._peers[key]
                gone = [k for k, v in self._visitors.items() if now - v.last_seen > VISITOR_FORGET]
                for key in gone:
                    del self._visitors[key]
                    self._inboxes.pop(key, None)
                went_offline = any(
                    not v.online and now - v.last_seen < VISITOR_STALE + 2.5
                    for v in self._visitors.values()
                )
                peers = [p for p in self._peers.values() if p.online or p.manual]
            if went_offline:
                self._bump()
            for job in self._jobs_snapshot():
                if not job.finished and all(
                    d.view_state() in FINAL for d in job.deliveries.values()
                ):
                    job.finished = now
            if now - last_hello >= HELLO_EVERY:
                last_hello = now
                for peer in peers:
                    self._executor.submit(self._hello, peer)
                for address in self.config.peers:
                    if now - manual_known.get(address, -1e9) >= HELLO_EVERY * 2:
                        manual_known[address] = now
                        self._executor.submit(self._connect_quietly, address)

    def _connect_quietly(self, address: str) -> None:
        with contextlib.suppress(MeshError, ValueError):
            self.connect(address)

    def _jobs_snapshot(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    # ---------------------------------------------------------------- views

    def devices_for(self, viewer: Viewer) -> list[dict[str, Any]]:
        """Everything ``viewer`` could send to, nearest first."""
        paired = self.trust.ids()
        out: list[dict[str, Any]] = []
        with self._lock:
            if not viewer.is_owner:
                ident = self.identity
                entry = _device(
                    ident.id, ident.name, ident.form, ident.platform, "app", online=True
                )
                entry["host"] = True
                entry["paired"] = viewer.trusted
                out.append(entry)
            for v in self._visitors.values():
                if v.online and v.id != viewer.id:
                    entry = _device(v.id, v.name, v.form, v.platform, "browser", online=True)
                    entry["via_name"] = self.identity.name
                    if viewer.is_owner:
                        entry["address"] = v.address
                        entry["paired"] = v.trusted
                    out.append(entry)
            for p in sorted(self._peers.values(), key=lambda p: (not p.online, p.name.lower())):
                if not p.online and p.id not in paired and not p.manual:
                    continue
                entry = _device(p.id, p.name, p.form, p.platform, "app", online=p.online)
                entry["paired"] = p.id in paired
                if p.accepts == "paired":
                    entry["accepts"] = "paired"
                if viewer.is_owner:
                    entry["address"] = p.address
                out.append(entry)
                if p.online:
                    for w in p.visitors:
                        if w["id"] == viewer.id:
                            continue
                        web = _device(
                            w["id"], w["name"], w["form"], w["platform"], "browser", online=True
                        )
                        web["via_name"] = p.name
                        out.append(web)
            if viewer.is_owner:
                known = {d["id"] for d in out}
                for trusted in self.trust.all():
                    if trusted.id not in known:
                        entry = _device(
                            trusted.id, trusted.name, "computer", "unknown", "app", online=False
                        )
                        entry["paired"] = True
                        out.append(entry)
        return out

    def _incoming_view(self, s: IncomingSession) -> dict[str, Any]:
        return {
            "id": s.id,
            "from": {k: s.origin.get(k) for k in ("id", "name", "form", "platform", "via_name")},
            "paired": s.paired,
            "state": s.state,
            "reason": s.reason,
            "total": s.total,
            "received": s.received,
            "files": [
                {
                    "name": f.name,
                    "size": f.size,
                    "mime": f.mime,
                    "kind": file_kind(f.name),
                    "state": f.state,
                    "received": f.received,
                    "saved_name": f.saved_name,
                }
                for f in s.files
            ],
            "expires_in": max(0, round(OFFER_TTL - (time.monotonic() - s.created)))
            if s.state == PENDING
            else None,
        }

    def _job_view(self, job: Job) -> dict[str, Any]:
        total = sum(int(f["size"]) for f in job.files)
        targets = []
        for target_id, d in job.deliveries.items():
            sent = d.sent
            if d.session is not None:
                sent = max(sent, d.session.received)
            targets.append(
                {
                    **d.target,
                    "id": target_id,
                    "state": d.view_state(),
                    "reason": d.reason or (d.session.reason if d.session else ""),
                    "sent": min(sent, total),
                }
            )
        return {
            "id": job.id,
            "files": job.files,
            "total": total,
            "targets": targets,
            "canceled": job.canceled,
            "finished": bool(job.finished),
        }

    def state_for(self, viewer: Viewer) -> dict[str, Any]:
        with self._lock:
            incoming = [
                self._incoming_view(s)
                for s in sorted(self._incoming.values(), key=lambda s: s.created)
                if s.target == viewer.id
            ]
            outgoing = [
                self._job_view(j)
                for j in sorted(self._jobs.values(), key=lambda j: j.created)
                if j.owner == viewer.id
            ]
        ident = self.identity
        state: dict[str, Any] = {
            "me": {
                "id": viewer.id,
                "name": viewer.name,
                "form": viewer.form,
                "platform": viewer.platform,
                "kind": viewer.kind,
                "paired": viewer.trusted,
            },
            "host": {
                "id": ident.id,
                "name": ident.name,
                "form": ident.form,
                "platform": ident.platform,
            },
            "devices": self.devices_for(viewer),
            "incoming": incoming,
            "outgoing": outgoing,
            "discovery": bool(self.discovery and self.discovery.working),
        }
        if viewer.is_owner:
            state["pairing"] = {"code": self.pair_code, "expires_in": self.pair_code_expires_in()}
            state["settings"] = {
                "auto_accept": self.config.auto_accept,
                "paired_only": self.config.paired_only,
            }
        elif self.has_inbox(viewer.id):
            store = self.inbox(viewer.id)
            state["inbox"] = [f.to_dict() for f in store.files()]
        else:
            state["inbox"] = []
        return state


# =================================================================== helpers


class _Watched:
    """Wraps a request body: counts bytes and stops when the transfer is canceled."""

    def __init__(
        self, stream: Readable, on_bytes: Callable[[int], None], stop: Callable[[], bool]
    ) -> None:
        self._stream = stream
        self._on_bytes = on_bytes
        self._stop = stop

    def read(self, size: int = -1) -> bytes:
        if self._stop():
            raise ConnectionAbortedError("transfer canceled")
        data = self._stream.read(size)
        self._on_bytes(len(data))
        return data


_EOF = object()


class _QueueReader:
    """A file-like object fed chunk by chunk from the fan-out loop."""

    def __init__(self, on_bytes: Callable[[int], None]) -> None:
        self.queue: queue.Queue[object] = queue.Queue(maxsize=16)
        self._buffer = b""
        self._done = False
        self.aborted = False
        self._on_bytes = on_bytes

    def read(self, size: int = -1) -> bytes:
        while not self._buffer and not self._done:
            if self.aborted:
                raise ConnectionAbortedError("transfer aborted")
            try:
                item = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is _EOF:
                self._done = True
            elif isinstance(item, bytes):
                self._buffer = item
        if self.aborted:
            raise ConnectionAbortedError("transfer aborted")
        if size is None or size < 0:
            data, self._buffer = self._buffer, b""
        else:
            data, self._buffer = self._buffer[:size], self._buffer[size:]
        self._on_bytes(len(data))
        return data


class _Sink:
    """Delivers one file to one receiver on its own thread."""

    def __init__(
        self, mesh: Mesh, job: Job, target_id: str, delivery: Delivery, index: int, size: int
    ) -> None:
        self.mesh = mesh
        self.job = job
        self.target_id = target_id
        self.delivery = delivery
        self.index = index
        self.size = size
        self.reader = _QueueReader(self._count)
        self.ok = False
        self.error = ""
        self._finished = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ot-send", daemon=True)

    @property
    def alive(self) -> bool:
        return not self._finished.is_set() and not self.delivery.canceled

    def _count(self, n: int) -> None:
        self.delivery.sent += n

    def start(self) -> None:
        if self.delivery.session is None:
            self.delivery.state = "sending"
        self._thread.start()

    def feed(self, chunk: bytes) -> None:
        deadline = time.monotonic() + STALL_TIMEOUT
        while self.alive:
            try:
                self.reader.queue.put(chunk, timeout=0.5)
                return
            except queue.Full:
                if time.monotonic() > deadline:
                    self.error = f"{self.delivery.target['name']} stopped responding."
                    self.reader.aborted = True
                    return

    def finish(self) -> None:
        while self.alive:
            try:
                self.reader.queue.put(_EOF, timeout=0.5)
                return
            except queue.Full:
                continue

    def abort(self, reason: str) -> None:
        self.error = self.error or reason
        self.reader.aborted = True

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        try:
            if self.delivery.session is not None:
                self.mesh.receive_file(
                    self.delivery.session.id, self.index, self.reader, self.size,
                    session=self.delivery.session,
                )  # fmt: skip
            else:
                self._put_remote()
            self.ok = True
        except Exception as exc:
            self.error = self.error or _describe_error(exc, self.delivery)
            log.debug("delivery to %s failed", self.target_id, exc_info=True)
        finally:
            self._finished.set()

    def _put_remote(self) -> None:
        peer = self.delivery.peer
        assert peer is not None  # noqa: S101
        conn = http.client.HTTPConnection(
            peer.host, peer.port, timeout=STALL_TIMEOUT, blocksize=CHUNK
        )
        try:
            conn.request(
                "PUT",
                f"{P2P}/offers/{self.delivery.remote_id}/files/{self.index}",
                body=self.reader,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(self.size),
                    "X-OT-Secret": self.delivery.remote_secret,
                },
                encode_chunked=False,
            )
            res = conn.getresponse()
            raw = res.read(100_000)
        finally:
            conn.close()
        if res.status != 201:
            try:
                data = json.loads(raw)
            except ValueError:
                data = {}
            if res.status == 410:
                self.delivery.state = CANCELED
            raise MeshError(
                res.status, "remote", _remote_message(data, "The receiver rejected the file.")
            )


def _describe_error(exc: Exception, delivery: Delivery) -> str:
    if isinstance(exc, StorageError):
        return str(exc)
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return f"Lost connection to {delivery.target.get('name', 'the receiver')}."
    return "Transfer failed."


def _target(device_id: str, name: str, form: str, platform: str, kind: str) -> dict[str, Any]:
    return {"id": device_id, "name": name, "form": form, "platform": platform, "kind": kind}


def _device(
    device_id: str, name: str, form: str, platform: str, kind: str, *, online: bool
) -> dict[str, Any]:
    return {
        "id": device_id,
        "name": name,
        "form": form,
        "platform": platform,
        "kind": kind,
        "online": online,
        "paired": False,
    }


def _clean_visitors(items: list[Any]) -> list[dict[str, str]]:
    out = []
    for item in items[:64]:
        if (
            isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item["id"].startswith("w-")
            and valid_id(item["id"])
        ):
            out.append(
                {
                    "id": item["id"],
                    "name": clean_name(item.get("name"), "Browser"),
                    "form": clean_form(item.get("form")),
                    "platform": clean_platform(item.get("platform")),
                }
            )
    return out


def _clean_files(files: Any) -> list[IncomingFile]:
    if not isinstance(files, list) or not files:
        raise MeshError(400, "no_files", "Choose at least one file.")
    if len(files) > MAX_FILES:
        raise MeshError(400, "too_many_files", f"Send {MAX_FILES} files or fewer at a time.")
    clean = []
    for item in files:
        if not isinstance(item, dict):
            raise MeshError(400, "bad_request", "Invalid file list.")
        size = item.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise MeshError(400, "bad_request", "Invalid file size.")
        try:
            name = safe_filename(str(item.get("name", "")))
        except StorageError as exc:
            raise MeshError(400, "invalid_name", str(exc)) from exc
        mime = str(item.get("mime") or "application/octet-stream")[:100]
        clean.append(IncomingFile(name=name, size=size, mime=mime))
    return clean


def _parse_address(address: str) -> tuple[str, int]:
    """``"192.168.1.20:5000"``, ``"http://laptop.local:5001/"`` or a bare host (port 5000)."""
    text = str(address).strip()
    for prefix in ("http://", "https://"):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
    text = text.split("/", 1)[0]
    host, _, port_text = text.partition(":")
    try:
        port = int(port_text) if port_text else 5000
    except ValueError:
        port = 0
    if not host or ":" in port_text or not 1 <= port <= 65535 or any(c in host for c in " @[]"):
        raise MeshError(400, "bad_address", "Enter an address like 192.168.1.20:5000.")
    return host, port


def _valid_nonce(value: object) -> bool:
    return (
        isinstance(value, str)
        and 16 <= len(value) <= 128
        and all(c in "0123456789abcdef" for c in value)
    )


def _remote_message(data: dict[str, Any], fallback: str) -> str:
    error = data.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return str(error["message"])[:200]
    return fallback


def visitor_identity(
    visitor_id: str | None, name: object, form: object, platform: object
) -> tuple[str, str, str, str]:
    """Normalise what a browser says about itself."""
    vid = str(visitor_id) if valid_id(visitor_id) else ""
    if not vid.startswith("w-"):
        vid = new_visitor_id()
    return vid, clean_name(name, "Browser"), clean_form(form), clean_platform(platform)
