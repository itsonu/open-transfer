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

import base64
import contextlib
import dataclasses
import hashlib
import http.client
import ipaddress
import json
import logging
import os
import queue
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass, field
from typing import Any

from open_transfer import __version__
from open_transfer.chunks import CHUNK_SIZE as RESUMABLE_CHUNK_SIZE
from open_transfer.chunks import ChunkSessions
from open_transfer.clipboard import IMAGE_MIME as CLIP_IMAGE_MIME
from open_transfer.clipboard import MAX_IMAGE_BYTES as CLIP_MAX_IMAGE
from open_transfer.clipboard import MAX_TEXT_BYTES as CLIP_MAX_TEXT
from open_transfer.clipboard import ClipboardStore
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
from open_transfer.history import History
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
from open_transfer.sync.engine import SyncEngine
from open_transfer.sync.store import SyncStore

log = logging.getLogger("open_transfer")

OFFER_TTL = 120.0  # seconds a receiver has to accept
PEER_STALE = 15.0  # an app we haven't heard from for this long is "not nearby"
PEER_FORGET = 120.0  # …and is dropped from the list (paired apps stay, greyed out)
VISITOR_STALE = 25.0  # browsers poll every 1.5 s, hidden tabs every 10 s
VISITOR_FORGET = 24 * 3600.0
HELLO_EVERY = 6.0
HELLO_TIMEOUT = 3.0  # seconds a hello may take before we give up on it
FINISHED_KEEP = 90.0  # finished transfers stay in the UI this long
PAIR_CODE_TTL = 600.0
STALL_TIMEOUT = 60.0  # a receiver that takes no data for this long is dropped
CHUNK = 256 * 1024
MAX_FILES = 1000
MAX_VISITORS = 500
MAX_INCOMING = 200
MAX_TEXT_BYTES = 200_000
P2P = "/api/p2p/v1"
SIGNAL_KINDS = {"offer", "answer", "bye"}
SIGNAL_MAX_BYTES = 64 * 1024
SIGNAL_MAX_QUEUED = 32
SIGNAL_TTL = 60.0

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
    left_at: float = 0.0  # when it last said goodbye
    manual: bool = False
    visitors: list[dict[str, str]] = field(default_factory=list)
    accepts: str = "everyone"
    rtt: float = -1.0  # round trip of the last hellos, in ms (smoothed); -1 = unknown

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
    direct: bool = False  # received browser-to-browser; it lives in the receiving browser
    duplicate: bool = False  # a file with this name and size is already here
    skipped: bool = False  # …and the receiver chose to keep theirs
    sha256: str = ""  # of what was written (checked against the sender's)
    verified: bool = False
    hasher: Any = field(default=None, repr=False)  # chunked: hash of the bytes so far, in order
    hashed: int = 0


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
    job: str = ""  # the sender's job id (to route direct-transfer signals back)
    sender_node: str = ""  # app that made the offer ("" when local)
    started: float = 0.0  # when the first byte arrived (for the speed in history)
    recorded: bool = False  # written to history
    on_duplicate: str = "rename"  # files already here: "rename" (keep both), "skip" or "replace"

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
    direct: bool = False  # the sender's browser is sending straight to the receiver's (WebRTC)
    skip: set[int] = field(default_factory=set)  # files the receiver already has and kept
    verified: set[int] = field(default_factory=set)  # files whose checksum the receiver confirmed

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
    # Files already relayed to every receiver: index → the fan-out result. The
    # staged chunks are gone by then, so this is how a resumed upload learns
    # it has nothing left to send (#44).
    relayed: dict[int, dict[str, Any]] = field(default_factory=dict)
    started: float = 0.0  # when the first byte went out (for the speed in history)
    recorded: bool = False  # written to history


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
        self.clipboard = ClipboardStore(state)
        self.history = History(state)
        self._clip_latest = ""  # newest clipboard item received from a paired device
        self._inbox_root = state / "inbox"
        # Big outgoing files are staged here chunk-by-chunk (browser → this app)
        # before being fanned out to the receivers. Crash-safe: a page reload
        # resumes the same upload id instead of starting over.
        self._relay_chunks = ChunkSessions(state / "relay-chunks")
        self._relay_meta: dict[str, dict[str, object]] = {}
        self._invites: dict[str, dict[str, object]] = {}
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
        self._mail: dict[str, list[tuple[float, dict[str, Any]]]] = {}
        self._rotate_code()
        self._rev = 0
        self.port = 0
        self.discovery: Discovery | None = None
        self._executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="ot-mesh")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hellos: set[Future[bool]] = set()
        self.sync = SyncEngine(
            me=lambda: self.identity.to_dict(),
            trust=self.trust,
            store=SyncStore(state),
            selected=lambda: set(self.clipboard.settings()["allowed"]),
            post=self._sync_post,
            online=self._online_ids,
            describe=self._describe_peer,
            submit=self._submit,
            meet=self._meet,
        )

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
        # Let hellos already on their way land before saying goodbye, so none of
        # them arrives after it and makes us look online again.
        with self._lock:
            in_flight = list(self._hellos)
        wait_futures(in_flight, timeout=HELLO_TIMEOUT)
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
        status = 0
        raw = b""
        try:
            conn.request(method, path, body=data or None, headers=all_headers)
            res = conn.getresponse()
            raw = res.read(2_000_000)
            status = res.status
        finally:
            conn.close()
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
        return status, parsed if isinstance(parsed, dict) else {}

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
            # A hello from loopback came through a local tunnel (adb forward,
            # SSH -L): it says nothing about where the peer really is.
            if not (_is_loopback(host) and peer.host and not _is_loopback(peer.host)):
                peer.host = host
            peer.port = port
            peer.version = str(info.get("version") or info.get("ver") or "")[:20]
            peer.accepts = "paired" if info.get("accepts") == "paired" else "everyone"
            peer.manual = peer.manual or manual
            peer.last_seen = time.monotonic()
            if isinstance(info.get("visitors"), list):
                peer.visitors = _clean_visitors(info["visitors"])
            if is_new:
                log.info("Found %s (%s) at %s", log_safe(peer.name), peer.platform, peer.address)
        self.trust.rename(peer_id, peer.name)
        if is_new and self.trust.get(peer_id):
            self._submit(lambda: self.sync.peer_online(peer_id))
        return peer

    def _on_packet(self, packet: dict[str, Any], src: str) -> None:
        if packet.get("id") == self.id or self._stop.is_set():
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
                    peer.left_at = time.monotonic()
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
            self._say_hello(peer)
        elif changed:
            self._say_hello(peer)

    def _say_hello(self, peer: Peer) -> None:
        """Say hello in the background, remembering it until it lands (see ``stop``)."""
        if self._stop.is_set():
            return
        try:
            future = self._executor.submit(self._hello, peer)
        except RuntimeError:  # the executor shut down meanwhile
            return
        with self._lock:
            self._hellos.add(future)
        future.add_done_callback(self._hello_done)

    def _hello_done(self, future: Future[bool]) -> None:
        with self._lock:
            self._hellos.discard(future)

    def _hello(self, peer: Peer) -> bool:
        if self._stop.is_set():
            return False
        started = time.monotonic()
        try:
            status, info = self._http(
                peer,
                "POST",
                f"{P2P}/hello",
                body=self.self_info(include_visitors=False),
                timeout=HELLO_TIMEOUT,
            )
        except OSError:
            return False
        if status != 200:
            return False
        with self._lock:
            known = self._peers.get(peer.id)
            said_goodbye = known is not None and known.left_at >= started
        if said_goodbye:
            return False  # they left while we were talking: the answer is stale
        updated = self._upsert_peer(info, peer.host, manual=peer.manual)
        if updated is not None:
            took = (time.monotonic() - started) * 1000
            with self._lock:
                updated.rtt = took if updated.rtt < 0 else updated.rtt * 0.7 + took * 0.3
        return True

    def handle_hello(self, info: dict[str, Any], host: str) -> dict[str, Any]:
        if self._stop.is_set():
            raise MeshError(503, "stopping", "This device is shutting down.")
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
        self._say_hello(peer)
        return peer

    # ------------------------------------------------------------ visitors

    def touch_visitor(
        self, visitor_id: str, *, name: str, form: str, platform: str, address: str, trusted: bool
    ) -> Visitor:
        with self._lock:
            visitor = self._visitors.get(visitor_id)
            changed = visitor is None or not visitor.online or visitor.name != name
            if visitor is None:
                if len(self._visitors) >= MAX_VISITORS:
                    # Evict the least-recently-seen visitor instead of growing forever.
                    oldest = min(self._visitors, key=lambda k: self._visitors[k].last_seen)
                    self._visitors.pop(oldest, None)
                    self._inboxes.pop(oldest, None)
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
        self._submit(lambda: self.sync.paired(device_id))
        return {"proof": pair_proof(code, "a", nonce_a, nonce_b, self.id, device_id)}

    def pair_with(self, code: str, address: str | None = None) -> Peer:
        """Pair with the app showing ``code`` (found on the network, or at ``address``)."""
        code = "".join(ch for ch in str(code) if ch.isdigit())
        if len(code) != 6:
            raise MeshError(400, "bad_code", "Enter the 6-digit code shown on the other device.")
        found = self.connect(address) if address else self._find_by_code(code)
        # Use one address for both steps: the shared entry's host can change under
        # us (we hear a device on each of its interfaces), and the other side
        # checks that both steps come from the same address.
        peer = dataclasses.replace(found, visitors=[])
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
        self._submit(lambda: self.sync.paired(peer.id))
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
        """Forget ``device_id``; with sync on, every member of the network forgets it too."""
        allowed = self.clipboard.settings()["allowed"]
        if device_id in allowed:
            self.clipboard.set_settings(allowed=[i for i in allowed if i != device_id])
        if not self.trust.get(device_id):
            return False
        if self.sync.active():
            self.sync.revoke(device_id)
            return True
        return self.trust.remove(device_id)

    # ---------------------------------------------------------------- sync

    def _submit(self, fn: Callable[[], Any]) -> Future[Any]:
        try:
            return self._executor.submit(fn)
        except RuntimeError:  # shutting down: do it here instead
            future: Future[Any] = Future()
            try:
                future.set_result(fn())
            except Exception as exc:
                future.set_exception(exc)
            return future

    def _online_ids(self) -> set[str]:
        with self._lock:
            return {p.id for p in self._peers.values() if p.online}

    def _describe_peer(self, device_id: str) -> dict[str, Any] | None:
        with self._lock:
            peer = self._peers.get(device_id)
            if peer is None:
                return None
            return {
                "id": peer.id,
                "name": peer.name,
                "form": peer.form,
                "platform": peer.platform,
                "address": peer.address,
            }

    def _meet(self, device_id: str, address: str) -> None:
        """A member introduced us to ``device_id`` at ``address``: find it there if we can't see it."""
        with self._lock:
            known = self._peers.get(device_id)
        if known is not None and known.online:
            return

        def look() -> None:
            try:
                host, port = _parse_address(address)
                probe = Peer("d-00000000", "", "computer", "unknown", host, port)
                status, info = self._http(probe, "GET", f"{P2P}/info", timeout=4)
            except (OSError, MeshError):
                return
            if status == 200 and info.get("id") == device_id:
                peer = self._upsert_peer(info, host, manual=True)
                if peer is not None:
                    self._say_hello(peer)

        self._submit(look)

    def _sync_post(self, dst: str, kind: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with self._lock:
            peer = self._peers.get(dst)
        if peer is None or not peer.online or self._stop.is_set():
            raise OSError("not nearby")
        path = f"{P2P}/sync/forward" if kind == "forward" else f"{P2P}/sync"
        return self._http(peer, "POST", path, body=body, timeout=10)

    # ----------------------------------------------------------- clipboard

    def clipboard_devices(self) -> list[dict[str, Any]]:
        """Paired apps, and whether this device shares its clipboard with each."""
        allowed = set(self.clipboard.settings()["allowed"])
        out = []
        with self._lock:
            for trusted in self.trust.all():
                peer = self._peers.get(trusted.id)
                out.append(
                    {
                        "id": trusted.id,
                        "name": peer.name if peer else trusted.name,
                        "online": bool(peer and peer.online),
                        "allowed": trusted.id in allowed,
                    }
                )
        return sorted(out, key=lambda d: d["name"].lower())

    def set_clipboard_settings(
        self,
        *,
        allowed: list[str] | None = None,
        max_items: int | None = None,
        max_age_days: int | None = None,
    ) -> dict[str, Any]:
        if allowed is not None:
            if not isinstance(allowed, list):
                raise MeshError(400, "bad_request", "Expected a list of devices.")
            if not set(allowed) <= self.trust.ids():
                raise MeshError(400, "not_paired", "Only paired devices can share a clipboard.")
        try:
            return self.clipboard.set_settings(
                allowed=allowed, max_items=max_items, max_age_days=max_age_days
            )
        except (ValueError, TypeError) as exc:
            raise MeshError(400, "bad_request", str(exc)) from None

    def share_clipboard(
        self, *, kind: str, text: str = "", data: bytes = b"", mime: str = ""
    ) -> dict[str, Any]:
        """Send a clipboard item to every paired device this one shares with."""
        allowed = set(self.clipboard.settings()["allowed"])
        # Don't bounce a clip straight back to the device it just came from.
        echo = next(
            (
                i["device"]["id"]
                for i in self.clipboard.items()
                if i["direction"] == "received"
                and i["kind"] == "text"
                and kind == "text"
                and i.get("text") == text
                and time.time() - i["at"] < 60
            ),
            None,
        )
        with self._lock:
            targets = [
                (device_id, self._peers.get(device_id))
                for device_id in sorted(allowed)
                if device_id != echo and self.trust.get(device_id)
            ]
        ident = self.identity
        body: dict[str, Any] = {"from": {"id": ident.id, "name": ident.name}, "kind": kind}
        if kind == "text":
            body["text"] = text
        else:
            body.update(mime=mime, data=base64.b64encode(data).decode())

        def deliver(peer: Peer | None, device_id: str) -> tuple[bool, str, str]:
            name = peer.name if peer else (self.trust.get(device_id) or ident).name
            if peer is None or not peer.online:
                return False, name, "Not nearby"
            try:
                status, reply = self._http(peer, "POST", f"{P2P}/clip", body=body, timeout=15)
            except OSError:
                return False, name, "Couldn’t reach it"
            if status == 200:
                return True, name, ""
            error = reply.get("error")
            if isinstance(error, dict) and error.get("code") == "not_allowed":
                return False, name, "It doesn’t share its clipboard with this device"
            return False, name, "Couldn’t send it"

        futures = [
            (device_id, self._executor.submit(deliver, peer, device_id))
            for device_id, peer in targets
        ]
        delivered: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        for device_id, future in futures:
            ok, name, reason = future.result()
            (delivered if ok else failed).append(
                {"id": device_id, "name": name, **({"reason": reason} if reason else {})}
            )
        names = ", ".join(d["name"] for d in delivered)
        self.history.add(
            {
                "kind": "clipboard",
                "direction": "sent",
                "title": "Image" if kind == "image" else "Text",
                "files": 0,
                "size": len(data) if kind == "image" else len(text.encode()),
                "status": DONE if delivered else "not_sent",
                "devices": [
                    *({"id": d["id"], "name": d["name"], "status": DONE} for d in delivered),
                    *({"id": f["id"], "name": f["name"], "status": FAILED} for f in failed),
                ],
            }
        )
        item = self.clipboard.add(
            kind=kind,
            direction="sent",
            device={"id": delivered[0]["id"] if len(delivered) == 1 else "", "name": names},
            text=text,
            data=data,
            mime=mime,
        )
        return {"item": item, "delivered": delivered, "failed": failed}

    def handle_clip(self, sender_id: str, verified: bool, data: dict[str, Any]) -> dict[str, Any]:
        """A paired device shares its clipboard with us."""
        trusted = self.trust.get(sender_id) if verified else None
        if trusted is None:
            raise MeshError(403, "not_paired", "Only paired devices can share a clipboard.")
        if not self.clipboard.allowed(sender_id):
            raise MeshError(403, "not_allowed", "This device doesn’t share its clipboard with you.")
        kind, text, raw, mime = _clean_clip(data)
        with self._lock:
            peer = self._peers.get(sender_id)
        name = peer.name if peer else trusted.name
        item = self.clipboard.add(
            kind=kind,
            direction="received",
            device={"id": sender_id, "name": name},
            text=text,
            data=raw,
            mime=mime,
        )
        with self._lock:
            self._clip_latest = item["id"]
        self.history.add(
            {
                "kind": "clipboard",
                "direction": "received",
                "title": "Image" if kind == "image" else "Text",
                "files": 0,
                "size": len(raw) if kind == "image" else len(text.encode()),
                "status": DONE,
                "devices": [{"id": sender_id, "name": name, "status": DONE}],
            }
        )
        log.info("Clipboard %s from %s", kind, log_safe(name))
        return {"ok": True}

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
        job: str = "",
        sender_node: str = "",
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
            job=str(job)[:64],
            sender_node=sender_node,
        )
        store = self.storage if owner_target else self.inbox(target)
        for f in session.files:
            try:
                f.duplicate = store.resolve(f.name).stat().st_size == f.size
            except (StorageError, OSError):
                f.duplicate = False
        problem = self._space_problem(session)
        if problem:
            session.state, session.reason, session.finished = DECLINED, problem, time.monotonic()
        elif owner_target and (self.config.auto_accept or paired):
            self._accept(session)
        with self._lock:
            live = [s for s in self._incoming.values() if s.state not in FINAL]
            if len(live) >= MAX_INCOMING:
                raise MeshError(429, "busy", "Too many unanswered transfers. Try again shortly.")
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

    def decide(
        self, session_id: str, viewer: Viewer, accept: bool, *, duplicates: str = "rename"
    ) -> IncomingSession:
        if duplicates not in {"rename", "skip", "replace"}:
            raise MeshError(400, "bad_request", "Keep both, skip or replace files already here.")
        session = self._session_for(session_id, viewer)
        with self._lock:
            if session.state != PENDING:
                raise MeshError(409, "already_decided", "This transfer was already answered.")
            if accept:
                problem = self._space_problem(session)
                if problem:
                    raise MeshError(507, "insufficient_storage", problem)
                session.on_duplicate = duplicates
                # Files first, the session's state last: a sender polling the
                # offer must never see "accepted" without what it can skip.
                for f in session.files:
                    f.state = "waiting"
                    if duplicates == "skip" and f.duplicate:
                        f.state, f.skipped = DONE, True
                        f.saved_name, f.received = f.name, f.size
                if all(f.skipped for f in session.files):
                    self._maybe_finish(session)
                else:
                    session.state = ACCEPTED
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
            "files": [
                {"state": f.state, "received": f.received, "skipped": f.skipped}
                for f in session.files
            ],
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
            skipped = item.skipped
            if item.state in {RECEIVING, DONE} and not skipped:
                raise MeshError(409, "duplicate", "This file is already being received.")
            if length is None:
                raise MeshError(411, "length_required", "Send a Content-Length header.")
            if length != item.size:
                raise MeshError(400, "size_mismatch", "The file size doesn’t match the offer.")
            if not skipped:
                item.state, item.received, item.error = RECEIVING, 0, ""
                session.state = RECEIVING
                session.started = session.started or time.monotonic()
        store = self.storage if session.target == self.id else self.inbox(session.target)
        if skipped:  # the receiver kept theirs: take the bytes, keep nothing
            while stream.read(CHUNK):
                pass
            return FileInfo.from_path(store.resolve(item.name))

        def count(n: int) -> None:
            item.received += n

        hasher = hashlib.sha256()
        reader = _Watched(_Hashing(stream, hasher), count, lambda: session.state == CANCELED)
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
        saved = self._place(store, session, item, saved)
        with self._lock:
            item.state, item.saved_name, item.received = DONE, saved.name, item.size
            item.sha256 = hasher.hexdigest()
            self._maybe_finish(session)
        log.info(
            "Received %s (%s bytes) from %s", log_safe(saved.name), saved.size,
            log_safe(session.origin.get("name")),
        )  # fmt: skip
        if session.target == self.id:
            self._emit("received", {"path": str(store.root / saved.name), "name": saved.name})
        return saved

    def _place(
        self, store: Storage, session: IncomingSession, item: IncomingFile, saved: FileInfo
    ) -> FileInfo:
        """Replace the file already here when the receiver chose to."""
        if session.on_duplicate != "replace" or not item.duplicate or saved.name == item.name:
            return saved
        try:
            os.replace(store.root / saved.name, store.root / item.name)
            return FileInfo.from_path(store.resolve(item.name))
        except (OSError, StorageError):
            log.warning("Couldn’t replace %s; kept both", log_safe(item.name))
            return saved

    def verify_file(self, session_id: str, index: int, secret: str, claimed: str) -> dict[str, Any]:
        """The sender's checksum of a file it sent: does what we wrote match?"""
        session = self._session_by_secret(session_id, secret)
        if not 0 <= index < len(session.files):
            raise MeshError(404, "not_found", "No such file in this transfer.")
        item = session.files[index]
        if item.skipped:
            return {"verified": True, "skipped": True}
        if item.state != DONE or not item.sha256:
            raise MeshError(409, "not_done", "This file hasn’t arrived yet.")
        if secrets.compare_digest(item.sha256, claimed.lower()):
            item.verified = True
            return {"verified": True}
        store = self.storage if session.target == self.id else self.inbox(session.target)
        if item.saved_name:
            with contextlib.suppress(StorageError, OSError):
                store.resolve(item.saved_name).unlink()
        with self._lock:
            item.state, item.saved_name, item.verified = FAILED, None, False
            item.error = "It arrived damaged (checksum mismatch) and was removed."
            session.state = RECEIVING  # let _maybe_finish decide again
            self._maybe_finish(session)
        log.warning("Removed %s: it arrived damaged", log_safe(item.name))
        raise MeshError(409, "integrity_failed", "The file arrived damaged.")

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
                job=job.id,
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
            "job": job.id,
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
            elif state in {ACCEPTED, RECEIVING} or (state == DONE and _all_skipped(data)):
                delivery.skip = {
                    i
                    for i, f in enumerate(data.get("files") or [])
                    if isinstance(f, dict) and f.get("skipped")
                }
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
        job.started = job.started or time.monotonic()
        kept = self._mark_kept(job, index)
        sinks = [
            _Sink(self, job, target_id, delivery, index, size)
            for target_id, delivery in job.deliveries.items()
            if delivery.view_state() in {ACCEPTED, "sending"}
            and index not in delivery.files_done
            and not delivery.direct
        ]
        if not sinks and not kept:
            kept = self._refresh_kept(job, index)
        if not sinks:
            if kept:  # every receiver already has this one
                while stream.read(CHUNK):
                    pass
                return self._upload_result(job, index, [])
            if any(d.view_state() in {"offering", "waiting"} for d in job.deliveries.values()):
                raise MeshError(409, "waiting", "Still waiting for the receivers to accept.")
            raise MeshError(409, "no_receivers", "Nobody accepted this transfer.")
        for sink in sinks:
            sink.start()
        received = 0
        hasher = hashlib.sha256()
        try:
            while True:
                chunk = stream.read(CHUNK)
                if not chunk:
                    break
                received += len(chunk)
                hasher.update(chunk)
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
        self._verify_sinks(sinks, index, _claimed_digest(hasher))
        return self._upload_result(job, index, sinks)

    def _mark_kept(self, job: Job, index: int) -> bool:
        """Receivers that already had file ``index`` and kept theirs: it's done for them."""
        kept = False
        for delivery in job.deliveries.values():
            skipped = index in delivery.skip
            if delivery.session is not None and index < len(delivery.session.files):
                skipped = skipped or delivery.session.files[index].skipped
            if skipped and delivery.view_state() in {ACCEPTED, "sending", DONE}:
                delivery.files_done.add(index)
                if delivery.session is None and len(delivery.files_done) == len(job.files):
                    delivery.state = DONE
                kept = True
        return kept

    def _refresh_kept(self, job: Job, index: int) -> bool:
        """Nobody left to send to? Ask the receiving apps once which files they kept:
        our view of their answer can be a poll behind."""
        for delivery in job.deliveries.values():
            if delivery.peer is None or not delivery.remote_id or index in delivery.skip:
                continue
            try:
                status, data = self._http(
                    delivery.peer,
                    "GET",
                    f"{P2P}/offers/{delivery.remote_id}",
                    headers={"X-OT-Secret": delivery.remote_secret},
                    timeout=5,
                )
            except OSError:
                continue
            files = data.get("files") if status == 200 else None
            if isinstance(files, list):
                delivery.skip |= {
                    i for i, f in enumerate(files) if isinstance(f, dict) and f.get("skipped")
                }
        return self._mark_kept(job, index)

    def _verify_sinks(self, sinks: list[_Sink], index: int, digest: str) -> None:
        for sink in sinks:
            if sink.ok and sink.delivery.session is None:
                problem = self._verify_remote(sink.delivery, index, digest)
                if problem:
                    sink.ok, sink.error = False, problem

    def _verify_remote(self, delivery: Delivery, index: int, digest: str) -> str:
        """Ask the receiving app whether what it wrote has our checksum ("" = fine)."""
        peer = delivery.peer
        if peer is None:
            return ""
        try:
            status, data = self._http(
                peer,
                "POST",
                f"{P2P}/offers/{delivery.remote_id}/files/{index}/verify",
                body={"sha256": digest},
                headers={"X-OT-Secret": delivery.remote_secret},
                timeout=STALL_TIMEOUT,
            )
        except OSError:
            return ""  # couldn't ask: says nothing about the file
        error = data.get("error")
        if isinstance(error, dict) and error.get("code") == "integrity_failed":
            return "It arrived damaged — retry to send it again."
        if status == 200:
            delivery.verified.add(index)
        return ""  # older apps don't check; that's not a failure

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

    # ------------------------------------------- chunked (resumable) uploads
    #
    # Big files are split by the browser into 2 MiB pieces. Each piece is a
    # plain PUT with ``Content-Range: bytes <start>-<end>/<total>`` and an
    # ``X-Upload-Id`` the browser picks (so a reload resumes the same id).
    # This app stages the pieces under ``relay-chunks/``; when the last byte
    # arrives the assembled file is fanned out to the receivers exactly like
    # a one-shot upload. One lost chunk costs one retry, not the whole file.

    def _relay_key(self, job_id: str, index: int, upload_id: str) -> str:
        return f"{job_id}:{index}:{upload_id}"

    def relay_status(
        self, job_id: str, viewer: Viewer, index: int, upload_id: str
    ) -> dict[str, Any]:
        job = self.job_for(job_id, viewer)
        if not 0 <= index < len(job.files):
            raise MeshError(404, "not_found", "No such file in this transfer.")
        if index in job.relayed:
            return dict(job.relayed[index])
        info = self._relay_chunks.status(upload_id)
        if info is None:
            return {"received": 0, "total": int(job.files[index]["size"]), "ranges": []}
        if int(info.get("total", 0)) != int(job.files[index]["size"]):
            return {"received": 0, "total": int(job.files[index]["size"]), "ranges": []}
        return {
            "received": int(info.get("received", 0)),
            "total": int(info.get("total", 0)),
            "ranges": info.get("ranges", []),
        }

    def relay_append(
        self, job_id: str, viewer: Viewer, index: int, upload_id: str, offset: int, data: bytes
    ) -> dict[str, Any]:
        """Store one chunk of a big outgoing file. Returns progress; fans out when complete."""
        from open_transfer.chunks import MAX_CHUNK_BYTES

        job = self.job_for(job_id, viewer)
        if job.canceled:
            raise MeshError(410, CANCELED, "This transfer was canceled.")
        if not 0 <= index < len(job.files):
            raise MeshError(404, "not_found", "No such file in this transfer.")
        if index in job.relayed:  # a resumed upload re-sending a piece: already done
            return dict(job.relayed[index])
        job.started = job.started or time.monotonic()
        size = int(job.files[index]["size"])
        name = str(job.files[index].get("name", "file"))
        if len(data) > MAX_CHUNK_BYTES:
            raise MeshError(413, "too_large", "Chunk is too big.")
        if offset < 0 or offset + len(data) > size:
            raise MeshError(400, "bad_range", "Chunk is outside the file.")
        limit = self.config.max_upload_size
        if limit and size > limit:
            raise MeshError(413, "too_large", "File is larger than this device accepts.")
        with self._lock:
            meta = self._relay_meta.get(upload_id)
            if meta is None:
                self._relay_chunks.touch(upload_id, name, size, 0)
                self._relay_meta[upload_id] = {
                    "job_id": job_id,
                    "index": index,
                    "total": size,
                    "name": name,
                }
            elif meta.get("job_id") != job_id or int(str(meta.get("index", -1))) != index:
                raise MeshError(400, "bad_request", "Upload id doesn’t belong to this file.")
        try:
            received = self._relay_chunks.append(upload_id, offset, data)
        except KeyError:
            # First chunk arrived with an id we haven't seen (e.g. after a
            # restart): create the session and retry once.
            self._relay_chunks.touch(upload_id, name, size, 0)
            try:
                received = self._relay_chunks.append(upload_id, offset, data)
            except ValueError as exc:
                raise MeshError(400, "bad_range", "Chunk is outside the file.") from exc
        except ValueError as exc:
            raise MeshError(400, "bad_range", "Chunk is outside the file.") from exc
        info: dict[str, Any] = dict(self._relay_chunks.status(upload_id) or {})
        if received < size:
            return {
                "received": received,
                "total": size,
                "done": False,
                "ranges": info.get("ranges", []),
            }
        return self._relay_complete(job, index, upload_id, size)

    def _relay_complete(self, job: Job, index: int, upload_id: str, size: int) -> dict[str, Any]:
        """The last chunk arrived: stream the assembled file to every receiver.

        Local targets (our owner/visitors) are written straight to disk.
        Remote apps get the file in 2 MiB ``Content-Range`` pieces when it is
        big (one failed piece is retried, not the whole file); small files
        keep the single-PUT fast path.
        """
        from open_transfer.chunks import CHUNK_THRESHOLD as _THRESHOLD

        path = self._relay_chunks.path(upload_id)
        if path is None:
            raise MeshError(404, "not_found", "Upload is no longer available.")
        kept = self._mark_kept(job, index)
        try:
            eligible = [
                (target_id, delivery)
                for target_id, delivery in job.deliveries.items()
                if delivery.view_state() in {ACCEPTED, "sending"}
                and index not in delivery.files_done
                and not delivery.direct
            ]
        except Exception:
            eligible = []
        if not eligible and not kept:
            kept = self._refresh_kept(job, index)
        if not eligible and kept:  # every receiver already has this one
            result = self._upload_result(job, index, [])
            self._relay_chunks.discard(upload_id)
            with self._lock:
                self._relay_meta.pop(upload_id, None)
            result.update({"received": size, "total": size, "done": True})
            job.relayed[index] = dict(result)
            return result
        if not eligible:
            self._relay_chunks.discard(upload_id)
            with self._lock:
                self._relay_meta.pop(upload_id, None)
            if any(d.view_state() in {"offering", "waiting"} for d in job.deliveries.values()):
                raise MeshError(409, "waiting", "Still waiting for the receivers to accept.")
            raise MeshError(409, "no_receivers", "Nobody accepted this transfer.")
        local = [(tid, d) for tid, d in eligible if d.session is not None]
        remote = [(tid, d) for tid, d in eligible if d.session is None]
        sinks: list[_Sink] = []
        if local:
            sinks = [_Sink(self, job, tid, d, index, size) for tid, d in local]
            for sink in sinks:
                sink.start()
            try:
                with path.open("rb") as src:
                    while True:
                        chunk = src.read(RESUMABLE_CHUNK_SIZE)
                        if not chunk:
                            break
                        live = [s for s in sinks if s.alive]
                        if not live or job.canceled:
                            break
                        for sink in live:
                            sink.feed(chunk)
            except OSError as exc:
                for sink in sinks:
                    sink.abort("Couldn’t read the staged file.")
                raise MeshError(500, "internal_error", "Couldn’t read the staged file.") from exc
            if job.canceled:
                for sink in sinks:
                    sink.abort("Canceled")
                for sink in sinks:
                    sink.join(10)
                self._relay_chunks.discard(upload_id)
                with self._lock:
                    self._relay_meta.pop(upload_id, None)
                raise MeshError(410, CANCELED, "This transfer was canceled.")
            for sink in sinks:
                sink.finish()
            for sink in sinks:
                sink.join(STALL_TIMEOUT * 2)
        # Remote apps: big files go chunk-by-chunk (app-to-app resume), small
        # ones keep the single streaming PUT.
        chunked_results: dict[str, dict[str, Any]] = {}
        if remote and not job.canceled:
            big = [(tid, d) for tid, d in remote if size > _THRESHOLD and d.peer is not None]
            small = [
                (tid, d) for tid, d in remote if not (size > _THRESHOLD and d.peer is not None)
            ]
            if small:
                small_sinks = [_Sink(self, job, tid, d, index, size) for tid, d in small]
                for sink in small_sinks:
                    sink.start()
                hasher = hashlib.sha256()
                try:
                    with path.open("rb") as src:
                        while True:
                            chunk = src.read(RESUMABLE_CHUNK_SIZE)
                            if not chunk:
                                break
                            hasher.update(chunk)
                            live = [s for s in small_sinks if s.alive]
                            if not live or job.canceled:
                                break
                            for sink in live:
                                sink.feed(chunk)
                except OSError as exc:
                    for sink in small_sinks:
                        sink.abort("Couldn’t read the staged file.")
                    raise MeshError(
                        500, "internal_error", "Couldn’t read the staged file."
                    ) from exc
                if not job.canceled:
                    for sink in small_sinks:
                        sink.finish()
                    for sink in small_sinks:
                        sink.join(STALL_TIMEOUT * 2)
                    self._verify_sinks(small_sinks, index, _claimed_digest(hasher))
                sinks.extend(small_sinks)
            for tid, delivery in big:
                if job.canceled or delivery.canceled:
                    continue
                assert delivery.peer is not None  # noqa: S101 - filtered above
                if delivery.session is None and delivery.state not in {"sending", ACCEPTED}:
                    delivery.state = "sending"
                ok, error, digest = self._push_chunked(
                    delivery.peer, delivery, path, size, job, index
                )
                if ok:
                    problem = self._verify_remote(delivery, index, digest)
                    if problem:
                        ok, error = False, problem
                push_sink = _Sink(self, job, tid, delivery, index, size)
                push_sink.ok = ok
                push_sink.error = error or ""
                sinks.append(push_sink)
                if ok:
                    delivery.sent = size
        if job.canceled:
            self._relay_chunks.discard(upload_id)
            with self._lock:
                self._relay_meta.pop(upload_id, None)
            raise MeshError(410, CANCELED, "This transfer was canceled.")
        result = self._upload_result(job, index, sinks)
        self._relay_chunks.discard(upload_id)
        with self._lock:
            self._relay_meta.pop(upload_id, None)
        result.update({"received": size, "total": size, "done": True})
        for tid, info in chunked_results.items():
            result.setdefault("targets", {}).setdefault(tid, info)
        job.relayed[index] = dict(result)
        return result

    def _push_chunked(
        self, peer: Peer, delivery: Delivery, path: object, size: int, job: Job, index: int
    ) -> tuple[bool, str, str]:
        """Send a staged file to one app in 2 MiB pieces with per-chunk retry.

        Returns (ok, error, checksum of what was sent)."""
        from pathlib import Path as _Path

        from open_transfer.chunks import CHUNK_SIZE as _CHUNK

        src = _Path(str(path))
        offset = 0
        hasher = hashlib.sha256()
        try:
            with src.open("rb") as fh:
                while offset < size:
                    if job.canceled or delivery.canceled or self._stop.is_set():
                        return False, "Canceled", ""
                    end = min(offset + _CHUNK, size)
                    fh.seek(offset)
                    data = fh.read(end - offset)
                    if not data:
                        return False, "Couldn’t read the staged file.", ""
                    for attempt in range(4):
                        conn = http.client.HTTPConnection(
                            peer.host, peer.port, timeout=STALL_TIMEOUT, blocksize=_CHUNK
                        )
                        try:
                            conn.request(
                                "PUT",
                                f"{P2P}/offers/{delivery.remote_id}/files/{index}",
                                body=data,
                                headers={
                                    "Content-Type": "application/octet-stream",
                                    "Content-Length": str(len(data)),
                                    "Content-Range": f"bytes {offset}-{offset + len(data) - 1}/{size}",
                                    "X-OT-Secret": delivery.remote_secret,
                                },
                                encode_chunked=False,
                            )
                            res = conn.getresponse()
                            raw = res.read(100_000)
                        except OSError:
                            if attempt >= 3:
                                return (
                                    False,
                                    f"Lost connection to {delivery.target.get('name', 'the receiver')}.",
                                    "",
                                )
                            time.sleep(0.4 * (2**attempt))
                            continue
                        finally:
                            conn.close()
                        if res.status in (201, 308):
                            offset += len(data)
                            delivery.sent = offset
                            hasher.update(data)
                            break
                        if res.status == 410:
                            delivery.state = CANCELED
                            return False, "Canceled by the receiver", ""
                        if res.status in (400, 403, 404, 409, 413):
                            try:
                                payload = json.loads(raw)
                            except ValueError:
                                payload = {}
                            return (
                                False,
                                _remote_message(payload, "The receiver rejected the file."),
                                "",
                            )
                        if attempt >= 3:
                            return (
                                False,
                                f"Lost connection to {delivery.target.get('name', 'the receiver')}.",
                                "",
                            )
                        time.sleep(0.4 * (2**attempt))
        except OSError:
            return False, "Couldn’t read the staged file.", ""
        return True, "", _claimed_digest(hasher)

    def receive_chunk(
        self,
        session_id: str,
        index: int,
        offset: int,
        data: bytes,
        total: int,
        *,
        secret: str,
    ) -> dict[str, Any]:
        """Store one chunk of an incoming file (app → app resume).

        Chunks are staged in this device's chunk store; when the last byte
        arrives the file is published exactly like a one-shot upload.
        """
        from open_transfer.chunks import MAX_CHUNK_BYTES

        session = self._session_by_secret(session_id, secret or "")
        with self._lock:
            if session.state in {DECLINED, EXPIRED, CANCELED}:
                raise MeshError(410, session.state, session.reason or "This transfer was stopped.")
            if session.state not in {ACCEPTED, RECEIVING}:
                raise MeshError(409, "not_accepted", "The receiver hasn’t accepted yet.")
            if not 0 <= index < len(session.files):
                raise MeshError(404, "not_found", "No such file in this transfer.")
            item = session.files[index]
            if item.state == DONE:
                return {"received": item.size, "total": item.size, "done": True}
            if item.state not in {"waiting", RECEIVING, FAILED}:
                raise MeshError(409, "not_accepted", "The receiver hasn’t accepted yet.")
            if total != item.size:
                raise MeshError(400, "size_mismatch", "The file size doesn’t match the offer.")
            if len(data) > MAX_CHUNK_BYTES:
                raise MeshError(413, "too_large", "Chunk is too big.")
            key = _incoming_chunk_key(session_id, index)
            item.state = RECEIVING
            session.state = RECEIVING
            session.started = session.started or time.monotonic()
        store = self.storage if session.target == self.id else self.inbox(session.target)
        if offset < 0 or offset + len(data) > total:
            raise MeshError(400, "bad_range", "Chunk is outside the file.")
        try:
            info = store.chunks.status(key)
        except ValueError:
            info = None
        if info is None:
            try:
                store.chunks.touch(key, item.name, total, 0)
            except ValueError as exc:
                raise MeshError(400, "bad_request", "Invalid upload.") from exc
        try:
            received = store.chunks.append(key, offset, data)
        except (KeyError, ValueError) as exc:
            raise MeshError(400, "bad_range", "Chunk is outside the file.") from exc
        with self._lock:  # apps send pieces in order: hash as they come
            if offset == 0:
                item.hasher, item.hashed = hashlib.sha256(), 0
            if item.hasher is not None and offset == item.hashed:
                item.hasher.update(data)
                item.hashed += len(data)
        status: dict[str, Any] = dict(store.chunks.status(key) or {})
        with self._lock:
            item.received = received
        if received < total:
            return {
                "received": received,
                "total": total,
                "done": False,
                "ranges": status.get("ranges", []),
            }
        # Last chunk: publish and mark done (same tail as receive_file).
        try:
            saved = store.finish_chunked(key, item.name, max_size=self.config.max_upload_size)
        except (StorageError, OSError, ValueError) as exc:
            with self._lock:
                item.state = FAILED
                item.error = str(exc) if isinstance(exc, StorageError) else "Interrupted"
                self._maybe_finish(session)
            raise
        digest = (
            item.hasher.hexdigest()
            if item.hasher is not None and item.hashed == total
            else _file_digest(store.root / saved.name)
        )
        saved = self._place(store, session, item, saved)
        with self._lock:
            item.state, item.saved_name, item.received = DONE, saved.name, item.size
            item.sha256, item.hasher = digest, None
            self._maybe_finish(session)
        if session.target == self.id:
            self._emit("received", {"path": str(store.root / saved.name), "name": saved.name})
        return {"received": total, "total": total, "done": True, "file": saved.to_dict()}

    def incoming_chunk_status(self, session_id: str, index: int, secret: str) -> dict[str, Any]:
        """Bytes already staged for one incoming file (app → app resume)."""
        session = self._session_by_secret(session_id, secret or "")
        if not 0 <= index < len(session.files):
            raise MeshError(404, "not_found", "No such file in this transfer.")
        store = self.storage if session.target == self.id else self.inbox(session.target)
        info = store.chunks.status(_incoming_chunk_key(session_id, index))
        if info is None:
            return {"received": 0, "total": session.files[index].size, "ranges": []}
        return {
            "received": int(info.get("received", 0)),
            "total": int(info.get("total", session.files[index].size)),
            "ranges": info.get("ranges", []),
        }

    # ------------------------------------------------- one-time invite links
    #
    # The QR code already carries the pairing code (rotates every 10 min, dies
    # after one use). Invite links extend that idea to browsers: the owner
    # mints a single-use URL (``/?invite=…``) that lets exactly one visitor
    # in — even past the PIN — then expires. Handy for guests: no PIN to read
    # out loud, no lingering access after they leave.

    def create_invite(self, *, ttl: float = 600.0, single_use: bool = True) -> dict[str, Any]:
        import secrets as _secrets

        token = _secrets.token_urlsafe(18)
        now = time.monotonic()
        with self._lock:
            # Prune expired/used tokens so the dict can't grow forever.
            for key, inv in list(self._invites.items()):
                created = float(str(inv.get("created", 0)))
                ttl = float(str(inv.get("ttl", 600)))
                if inv.get("used") or now - created > ttl:
                    self._invites.pop(key, None)
            self._invites[token] = {
                "created": now,
                "ttl": ttl,
                "single_use": single_use,
                "used": False,
            }
        return {"token": token, "expires_in": int(ttl), "single_use": single_use}

    def redeem_invite(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            inv = self._invites.get(token)
            if inv is None:
                return False
            if inv.get("used"):
                return False
            created = float(str(inv.get("created", 0)))
            ttl = float(str(inv.get("ttl", 600)))
            if time.monotonic() - created > ttl:
                self._invites.pop(token, None)
                return False
            if inv.get("single_use"):
                inv["used"] = True
            return True

    def invite_info(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            live = sum(
                1
                for inv in self._invites.values()
                if not inv.get("used")
                and now - float(str(inv.get("created", 0))) <= float(str(inv.get("ttl", 600)))
            )
        return {"active": live}

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

    def retry_job(self, job_id: str, viewer: Viewer, targets: list[str] | None = None) -> Job:
        """Re-offer a finished job's files to targets that didn't get them.

        Used for "Retry" after a decline / failure / expiry: creates a brand
        new job (fresh offers) reusing the old job's file list, so the
        receiver is asked again and can accept the second time. Only targets
        in a retryable state (declined/failed/expired/canceled) are retried;
        done/sending targets are left alone.
        """
        job = self.job_for(job_id, viewer)
        retryable = {DECLINED, EXPIRED, FAILED, CANCELED}
        wanted = set(targets) if targets is not None else set(job.deliveries)
        ids: list[str] = []
        for target_id, delivery in job.deliveries.items():
            if target_id not in wanted:
                continue
            if delivery.view_state() in retryable:
                ids.append(target_id)
        if not ids:
            raise MeshError(
                409, "nothing_to_retry", "There’s nothing to retry — nobody declined or failed."
            )
        files = [
            {"name": f["name"], "size": f["size"], "mime": f.get("mime", "")} for f in job.files
        ]
        return self.create_job(viewer, ids, files)

    # ------------------------------------------------- direct (browser↔browser)
    #
    # Browsers can't listen for connections, but two browsers can talk directly
    # over WebRTC once they have swapped an offer and an answer. The apps carry
    # those few messages (to the right app, then to the page), then step aside.
    # Only the people already part of an accepted transfer can exchange them.

    def _post_mail(self, to: str, message: dict[str, Any]) -> None:
        now = time.monotonic()
        with self._lock:
            box = [m for m in self._mail.get(to, []) if now - m[0] < SIGNAL_TTL]
            box.append((now, message))
            self._mail[to] = box[-SIGNAL_MAX_QUEUED:]
            # Bound the number of mailboxes so fresh IDs can't grow memory forever.
            while len(self._mail) > MAX_VISITORS:
                oldest = min(
                    self._mail, key=lambda k: self._mail[k][0][0] if self._mail[k] else now
                )
                self._mail.pop(oldest, None)

    def take_signals(self, viewer: Viewer) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            box = self._mail.pop(viewer.id, [])
        return [m for at, m in box if now - at < SIGNAL_TTL]

    def signal(self, viewer: Viewer, body: dict[str, Any]) -> None:
        """A page sends a WebRTC message to the other end of one of its transfers."""
        kind = body.get("kind")
        sdp = body.get("sdp", "")
        if kind not in SIGNAL_KINDS or not isinstance(sdp, str) or len(sdp) > SIGNAL_MAX_BYTES:
            raise MeshError(400, "bad_signal", "Invalid connection message.")
        message: dict[str, Any] = {"kind": kind, "sdp": sdp}
        if body.get("job"):
            # Sender → receiver.
            job = self.job_for(str(body["job"]), viewer)
            target = str(body.get("target", ""))
            delivery = job.deliveries.get(target)
            if delivery is None or delivery.view_state() not in {ACCEPTED, "sending"}:
                raise MeshError(409, "not_accepted", "That device hasn’t accepted this transfer.")
            if delivery.session is not None:
                self._post_mail(target, {**message, "session": delivery.session.id})
            elif delivery.peer is not None:
                self._send_signal(
                    delivery.peer,
                    {**message, "dir": "to_receiver", "session": delivery.remote_id,
                     "secret": delivery.remote_secret},
                )  # fmt: skip
            return
        # Receiver → sender.
        session = self._session_for(str(body.get("session", "")), viewer)
        if session.state not in {ACCEPTED, RECEIVING}:
            raise MeshError(409, "not_accepted", "This transfer isn’t active.")
        reply = {**message, "job": session.job, "target": session.target}
        if session.local:
            self._post_mail(str(session.origin.get("id", "")), reply)
            return
        with self._lock:
            peer = self._peers.get(session.sender_node)
        if peer is None:
            raise MeshError(404, "unreachable", "The sending device isn’t nearby anymore.")
        self._send_signal(peer, {**reply, "dir": "to_sender", "secret": session.secret})

    def _send_signal(self, peer: Peer, body: dict[str, Any]) -> None:
        try:
            status, data = self._http(peer, "POST", f"{P2P}/signal", body=body, timeout=5)
        except OSError as exc:
            raise MeshError(502, "unreachable", f"Couldn’t reach {peer.name}.") from exc
        if status != 200:
            raise MeshError(
                status, "signal_failed", _remote_message(data, "Connection message refused.")
            )

    def handle_signal(self, body: dict[str, Any]) -> None:
        """A WebRTC message from another app, for one of our pages."""
        kind = body.get("kind")
        sdp = body.get("sdp", "")
        if kind not in SIGNAL_KINDS or not isinstance(sdp, str) or len(sdp) > SIGNAL_MAX_BYTES:
            raise MeshError(400, "bad_signal", "Invalid connection message.")
        message = {"kind": kind, "sdp": sdp}
        secret = str(body.get("secret", ""))
        if body.get("dir") == "to_receiver":
            session = self._session_by_secret(str(body.get("session", "")), secret)
            self._post_mail(session.target, {**message, "session": session.id})
        elif body.get("dir") == "to_sender":
            with self._lock:
                job = self._jobs.get(str(body.get("job", "")))
            target = str(body.get("target", ""))
            delivery = job.deliveries.get(target) if job else None
            if (
                job is None
                or delivery is None
                or not delivery.remote_secret
                or not secrets.compare_digest(delivery.remote_secret, secret)
            ):
                raise MeshError(404, "not_found", "Unknown transfer.")
            self._post_mail(job.owner, {**message, "job": job.id, "target": target})
        else:
            raise MeshError(400, "bad_signal", "Invalid connection message.")

    def set_direct(
        self, job_id: str, viewer: Viewer, target_id: str, state: str, sent: int = 0
    ) -> None:
        """The sender's page reports how sending straight to ``target_id`` is going."""
        job = self.job_for(job_id, viewer)
        delivery = job.deliveries.get(target_id)
        if delivery is None:
            raise MeshError(404, "not_found", "That device isn’t part of this transfer.")
        if state == "trying":
            if delivery.view_state() not in {ACCEPTED, "sending"}:
                raise MeshError(409, "not_accepted", "That device hasn’t accepted this transfer.")
            delivery.direct = True
        elif state == "failed":
            delivery.direct = False  # the files go the usual way instead
        elif state == "progress":
            delivery.sent = max(delivery.sent, int(sent))
        elif state == "done":
            delivery.sent = sum(int(f["size"]) for f in job.files)
            delivery.files_done = set(range(len(job.files)))
            if delivery.session is None:
                delivery.state = DONE
        else:
            raise MeshError(400, "bad_request", "Unknown state.")
        if all(d.view_state() in FINAL for d in job.deliveries.values()):
            job.finished = time.monotonic()

    def direct_received(
        self, session_id: str, viewer: Viewer, index: int, received: int, done: bool
    ) -> None:
        """The receiving page reports a file arriving straight from the sender's browser."""
        session = self._session_for(session_id, viewer)
        with self._lock:
            if session.state not in {ACCEPTED, RECEIVING}:
                raise MeshError(410, session.state, session.reason or "This transfer was stopped.")
            if not 0 <= index < len(session.files):
                raise MeshError(404, "not_found", "No such file in this transfer.")
            item = session.files[index]
            if item.state == DONE:
                return
            item.received = max(0, min(item.size, int(received)))
            session.state = RECEIVING
            if done:
                item.state, item.received, item.direct = DONE, item.size, True
                item.saved_name = item.name
                self._maybe_finish(session)

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
                    elif session.state == ACCEPTED and now - session.created > OFFER_TTL * 2:
                        # Accepted but sender never started uploading: don't leak it.
                        session.state, session.reason, session.finished = (
                            EXPIRED,
                            "No answer",
                            now,
                        )
                    elif session.state == RECEIVING and now - session.created > 3600:
                        # Stalled mid-transfer (sender vanished after ACCEPTED).
                        session.state, session.reason, session.finished = (
                            FAILED,
                            "Transfer stalled.",
                            now,
                        )
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
            self._record_finished()
            if now - last_hello >= HELLO_EVERY:
                last_hello = now
                for peer in peers:
                    self._say_hello(peer)
                for address in self.config.peers:
                    if now - manual_known.get(address, -1e9) >= HELLO_EVERY * 2:
                        manual_known[address] = now
                        self._executor.submit(self._connect_quietly, address)
            try:
                self.sync.tick()
            except Exception:
                log.warning("Clipboard sync housekeeping failed", exc_info=True)

    def _connect_quietly(self, address: str) -> None:
        with contextlib.suppress(MeshError, ValueError):
            self.connect(address)

    def _jobs_snapshot(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    # -------------------------------------------------------------- history

    def _record_finished(self) -> None:
        """Write this device's own finished transfers to its history (once each)."""
        with self._lock:
            jobs = [
                j
                for j in self._jobs.values()
                if j.owner == self.id and j.finished and not j.recorded
            ]
            sessions = [
                s
                for s in self._incoming.values()
                if s.target == self.id and s.state in FINAL and not s.recorded
            ]
            for job in jobs:
                job.recorded = True
            for session in sessions:
                session.recorded = True
        for job in jobs:
            self.history.add(self._job_entry(job))
        for session in sessions:
            self.history.add(self._session_entry(session))

    def _job_entry(self, job: Job) -> dict[str, Any]:
        total = sum(int(f["size"]) for f in job.files)
        devices = [
            {
                "id": target_id,
                "name": str(d.target.get("name", "")),
                "status": d.view_state(),
                **({"reason": d.reason} if d.reason else {}),
            }
            for target_id, d in job.deliveries.items()
        ]
        states = {d["status"] for d in devices}
        status = states.pop() if len(states) == 1 else "partial" if DONE in states else FAILED
        took = job.finished - job.started if job.started else 0
        return {
            "kind": "transfer",
            "direction": "sent",
            "title": _title(job.files),
            "files": len(job.files),
            "size": total,
            "status": status,
            "devices": devices,
            "speed": round(total / took) if took > 0 and status in {DONE, "partial"} else 0,
            "ref": job.id,
        }

    def _session_entry(self, session: IncomingSession) -> dict[str, Any]:
        took = session.finished - session.started if session.started else 0
        files: list[dict[str, Any]] = [
            {"name": f.saved_name or f.name, "size": f.size} for f in session.files
        ]
        return {
            "kind": "transfer",
            "direction": "received",
            "title": _title(files),
            "files": len(files),
            "size": session.total,
            "status": session.state,
            "devices": [
                {
                    "id": str(session.origin.get("id", "")),
                    "name": str(session.origin.get("name", "")),
                    "status": session.state,
                }
            ],
            "speed": round(session.total / took) if took > 0 and session.state == DONE else 0,
            "ref": session.id,
            **(
                {"image": files[0]["name"]}
                if len(files) == 1 and file_kind(str(files[0]["name"])) == "image"
                else {}
            ),
        }

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
                if p.online and p.rtt >= 0:
                    entry["rtt"] = round(p.rtt)
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
                    "direct": f.direct,
                    "duplicate": f.duplicate,
                    "skipped": f.skipped,
                    "sha256": f.sha256,
                    "verified": f.verified,
                    "error": f.error,
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
                    "direct": d.direct,
                    "path": _path(d),
                    "verified": d.session is None and len(d.verified | d.skip) == len(job.files),
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
            "signals": len(self._mail.get(viewer.id, [])),
        }
        if viewer.is_owner:
            state["pairing"] = {"code": self.pair_code, "expires_in": self.pair_code_expires_in()}
            state["history"] = {"latest": self.history.latest()}
            state["clipboard"] = {
                "latest": self._clip_latest,
                "sharing": len(self.clipboard.settings()["allowed"]),
            }
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


class _Hashing:
    """Wraps a stream and hashes what's read through it."""

    def __init__(self, stream: Readable, hasher: Any) -> None:
        self._stream = stream
        self._hasher = hasher

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        self._hasher.update(data)
        return data


def _claimed_digest(hasher: Any) -> str:
    """The checksum a sender reports for what it sent."""
    return str(hasher.hexdigest())


def _file_digest(path: Any) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _all_skipped(data: dict[str, Any]) -> bool:
    files = data.get("files") or []
    return bool(files) and all(isinstance(f, dict) and f.get("skipped") for f in files)


def _title(files: list[dict[str, Any]]) -> str:
    return str(files[0].get("name", "file")) if len(files) == 1 else f"{len(files)} files"


def _path(delivery: Delivery) -> str:
    """How a delivery travels: "direct" (browser to browser), "local" (on this
    app), "via" (to a browser through its app) or "app" (app to app)."""
    if delivery.direct:
        return "direct"
    if delivery.session is not None:
        return "local"
    return "via" if delivery.target.get("kind") == "browser" else "app"


def _clean_clip(data: dict[str, Any]) -> tuple[str, str, bytes, str]:
    """(kind, text, image bytes, mime) of a clipboard item, or a MeshError."""
    kind = data.get("kind")
    if kind == "text":
        text = data.get("text")
        if not isinstance(text, str) or not text:
            raise MeshError(400, "bad_request", "Nothing to share.")
        if len(text.encode()) > CLIP_MAX_TEXT:
            raise MeshError(413, "too_large", "That text is too long to share (1 MB max).")
        return "text", text, b"", ""
    if kind == "image":
        mime = data.get("mime")
        if mime not in CLIP_IMAGE_MIME:
            raise MeshError(
                400, "bad_request", "Only PNG, JPEG, GIF and WebP images can be shared."
            )
        encoded = data.get("data")
        if not isinstance(encoded, str) or len(encoded) > CLIP_MAX_IMAGE * 4 // 3 + 8:
            raise MeshError(413, "too_large", "That image is too big to share (10 MB max).")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise MeshError(400, "bad_request", "That image couldn’t be read.") from None
        if not raw or len(raw) > CLIP_MAX_IMAGE:
            raise MeshError(413, "too_large", "That image is too big to share (10 MB max).")
        return "image", "", raw, str(mime)
    raise MeshError(400, "bad_request", "Share text or an image.")


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


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _incoming_chunk_key(session_id: str, index: int) -> str:
    """Chunk-store id for one incoming file (the store only takes ``[A-Za-z0-9_-]``)."""
    return f"{session_id}-{index}"


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
        raw_mime = str(item.get("mime") or "application/octet-stream")[:100]
        mime = _clean_mime(raw_mime)
        # Text snippets ride the same pipeline as files (sender turns typed
        # text into a .txt file); flag them so receivers can preview + copy.
        kind = "text" if (mime == "text/plain" and name.lower().endswith(".txt")) else ""
        clean.append(IncomingFile(name=name, size=size, mime=mime))
        if kind:
            clean[-1].error = ""
    return clean


def _clean_mime(value: str) -> str:
    """Validate a client-supplied MIME type; fall back to octet-stream."""
    import re as _re

    if _re.fullmatch(
        r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*", value.strip().lower()
    ):
        return value.strip().lower()[:100]
    return "application/octet-stream"


def _parse_address(address: str) -> tuple[str, int]:
    """``"192.168.1.20:5000"``, ``"http://laptop.local:5001/"`` or a bare host (port 5000)."""
    text = str(address).strip()
    for prefix in ("http://", "https://"):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
    text = text.split("/", 1)[0].strip()
    host, port_text = "", ""
    if text.startswith("["):
        # IPv6 literal: [::1] or [::1]:5000
        end = text.find("]")
        if end == -1:
            raise MeshError(400, "bad_address", "Enter an address like 192.168.1.20:5000.")
        host = text[1:end]
        rest = text[end + 1 :]
        if rest.startswith(":"):
            port_text = rest[1:]
        elif rest:
            raise MeshError(400, "bad_address", "Enter an address like 192.168.1.20:5000.")
    else:
        if text.count(":") > 1:
            # Bare IPv6 literal without port (e.g. ::1): accept only if it
            # really is an IP literal, so "a:1:2" is still rejected.
            import ipaddress as _ip

            try:
                _ip.ip_address(text)
            except ValueError:
                raise MeshError(
                    400, "bad_address", "Enter an address like 192.168.1.20:5000."
                ) from None
            host, port_text = text, ""
        else:
            host, _, port_text = text.partition(":")
    try:
        port = int(port_text) if port_text else 5000
    except ValueError:
        port = 0
    if not host or not 1 <= port <= 65535 or any(c in host for c in " @[]"):
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
