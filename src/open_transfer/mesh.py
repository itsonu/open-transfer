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
import dataclasses
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

from open_transfer import __version__, network, srp
from open_transfer.config import Config
from open_transfer.devices import (
    IdentityStore,
    SignatureChecker,
    TrustStore,
    clean_form,
    clean_name,
    clean_platform,
    derive_pair_key,
    new_pair_code,
    new_visitor_id,
    session_mac,
    sign,
    valid_id,
    verify_response,
)
from open_transfer.discovery import Discovery
from open_transfer.history import (
    DISCONNECT_CODES,
    PARTIAL,
    FileRecord,
    History,
    RecipientRecord,
    TransferRecord,
    lifecycle_state,
    summarize,
)
from open_transfer.lifecycle import can_move, describe
from open_transfer.security import RateLimiter, log_safe
from open_transfer.storage import (
    FileInfo,
    IncompleteUpload,
    InsufficientStorage,
    Readable,
    Storage,
    StorageError,
    TooLarge,
    file_kind,
    safe_filename,
)

log = logging.getLogger("open_transfer")

OFFER_TTL = 120.0  # seconds a receiver has to accept
SENDER_GRACE = 15.0  # the sender hears about an expiry second; it gives up a little later
ACCEPT_TTL = 90.0  # an accepted transfer with no first byte by then has lost its sender
STATUS_KEEP = 600.0  # finished transfers still answer status questions this long
RECONCILE_TRIES = 3
PEER_STALE = 15.0  # an app we haven't heard from for this long is "not nearby"
PEER_FORGET = 120.0  # …and is dropped from the list (paired apps stay, greyed out)
VISITOR_STALE = 25.0  # browsers poll every 1.5 s, hidden tabs every 10 s
VISITOR_FORGET = 24 * 3600.0
HELLO_EVERY = 6.0
BYE_QUIET = 10.0  # after a bye, only our own hello brings a device back
FINISHED_KEEP = 90.0  # finished transfers stay in the UI this long
HISTORY_PRUNE_EVERY = 3600.0
PAIR_CODE_TTL = 600.0
PAIR_WINDOW = 30.0  # the code is accepted this long after the page last renewed the window
PAIR_SESSION_TTL = 120.0  # one SRP attempt
PAIR_CONFIRM_TTL = 60.0  # how long "Pair with …? Allow / Deny" waits
PAIRING_ADVERT_STALE = 35.0  # a device's "my pairing window is open" is believed this long
VERIFY_EVERY = 10.0  # at most one address check per device and address this often
STALL_TIMEOUT = 60.0  # a receiver that takes no data for this long is dropped
CHUNK = 256 * 1024
MAX_FILES = 1000
P2P = "/api/p2p/v1"
SIGNAL_KINDS = {"offer", "answer", "bye"}
SIGNAL_MAX_BYTES = 64 * 1024
SIGNAL_MAX_QUEUED = 32
SIGNAL_TTL = 60.0

# Incoming session states
PENDING, ACCEPTED, RECEIVING, DONE = "pending", "accepted", "receiving", "done"
DECLINED, EXPIRED, CANCELED, FAILED = "declined", "expired", "canceled", "failed"
FINAL = {DONE, PARTIAL, DECLINED, EXPIRED, CANCELED, FAILED}
#: Sender-side codes for the ``error.code`` of a refused offer.
OFFER_ERRORS = {
    "paired_only": (DECLINED, "permission_denied"),
    "insufficient_storage": (DECLINED, "insufficient_storage"),
    "not_here": (FAILED, "destination_unavailable"),
    "bad_request": (FAILED, "invalid_request"),
    "busy": (FAILED, "receiver_unreachable"),
}

Listener = Callable[[str, dict[str, Any]], None]


class MeshError(StorageError):
    """An error with an HTTP status, shown to the user as-is."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.extra = extra


class IdentityMismatch(ConnectionError):
    """A paired device's answer wasn't signed with our pair key (or it unpaired us)."""

    def __init__(self, message: str, *, unpaired: bool = False) -> None:
        super().__init__(message)
        self.unpaired = unpaired


# ===================================================================== models


@dataclass
class PairSession:
    """One attempt by another device to pair with us (we show the code)."""

    id: str
    device: dict[str, Any]  # the device asking
    client: str  # its address
    server: srp.Server
    created: float = field(default_factory=time.monotonic)
    state: str = "proving"  # proving → confirming → allowed | denied | expired | failed
    key: bytes = b""
    confirm_by: float = 0.0


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
    pairing_at: float = 0.0  # when it last said its pairing window is open

    @property
    def pairing(self) -> bool:
        return self.pairing_at > 0 and time.monotonic() - self.pairing_at < PAIRING_ADVERT_STALE

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
    reason_code: str = ""
    direct: bool = False  # received browser-to-browser; it lives in the receiving browser


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
    reason_code: str = ""
    finished: float = 0.0
    deadline: float = 0.0  # accepted: when it fails without a first byte
    last_activity: float = 0.0  # receiving: last time any byte arrived
    created_at: float = field(default_factory=time.time)  # wall clock, for history
    started_at: float | None = None
    finished_at: float | None = None  # wall clock
    local: bool = False  # offered by our own owner/visitor (no HTTP involved)
    source: str = ""  # address the offer came from (rate limiting)
    job: str = ""  # the sender's job id (to route direct-transfer signals back)
    sender_node: str = ""  # app that made the offer ("" when local)

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
    reason_code: str = ""
    sent: int = 0
    files_done: set[int] = field(default_factory=set)
    files_failed: dict[int, str] = field(default_factory=dict)  # index -> reason code
    canceled: bool = False  # stop feeding this receiver (its final state may follow later)
    direct: bool = False  # the sender's browser is sending straight to the receiver's (WebRTC)
    deadline: float = 0.0  # accepted: when it fails without a first byte
    last_activity: float = 0.0  # sending: last time the receiver took any byte
    busy: int = 0  # files being streamed to it right now
    started_at: float | None = None  # wall clock: first byte to this receiver
    finished_at: float | None = None  # wall clock: its final state

    def view_state(self) -> str:
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
    created_at: float = field(default_factory=time.time)  # wall clock, for history
    started_at: float | None = None


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
        self.history = History(state / "history.db")
        self.history.interrupt_unfinished()
        self.history.prune()
        self._recorded: dict[str, tuple[Any, ...]] = {}  # last snapshot saved, per live transfer
        # Ended sessions, kept for the sender's status questions: id -> (secret, status, ended)
        self._finished_status: dict[str, tuple[str, dict[str, Any], float]] = {}
        self._inbox_root = state / "inbox"
        self._lock = threading.RLock()
        self._peers: dict[str, Peer] = {}
        self._byes: dict[str, float] = {}  # device id -> when it said goodbye
        self._visitors: dict[str, Visitor] = {}
        self._inboxes: dict[str, Storage] = {}
        self._incoming: dict[str, IncomingSession] = {}
        self._jobs: dict[str, Job] = {}
        self._listeners: list[Listener] = []
        self._signatures = SignatureChecker()
        self._pair_limiter = RateLimiter(attempts=5, window=60)
        self._pair_failures = 0
        self._pair_window_until = 0.0
        self._pair_sessions: dict[str, PairSession] = {}
        self._verifying: dict[tuple[str, str, int], float] = {}
        self._multicast_heard = 0.0  # last time another device's datagram arrived
        self._mail: dict[str, list[tuple[float, dict[str, Any]]]] = {}
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
        """Shut down. Closing the app during a transfer is a failure, not a cancel:
        the transfer ends ``failed``/``app_closed`` here, and the receivers are
        told (``failed``/``sender_disconnected`` there), so it shows up as
        something to send again on both sides. A crash ends as ``app_restart``
        on the next start (History.interrupt_unfinished).
        """
        self._stop.set()
        with self._lock:
            jobs = list(self._jobs.values())
            sessions = [s for s in self._incoming.values() if s.state not in FINAL]
            for session in sessions:
                self._move_session(session, FAILED, "app_closed")
        tellers: list[threading.Thread] = []
        for job in jobs:
            if not job.finished:
                tellers += self.cancel_job(job.id, job.owner, quiet=True, code="app_closed")
        deadline = time.monotonic() + 3
        for thread in tellers:
            thread.join(max(0.0, deadline - time.monotonic()))
        if self._thread:
            self._thread.join(timeout=5)
        # Goodbye comes last: a hello still in flight would otherwise land after
        # it and show us online again. Every task here is a short HTTP call.
        self._executor.shutdown(wait=True, cancel_futures=True)
        if self.discovery:
            self.discovery.stop()
        self._record_all()
        self.history.close()

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
            "pairing": self.pairing_open,
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
            "pairing": self.pairing_open,
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
        auth = ""
        if trusted:
            auth = sign(bytes.fromhex(trusted.key), method, path, self.id, data)
            all_headers["X-OT-From"] = self.id
            all_headers["X-OT-Auth"] = auth
        all_headers.update(headers or {})
        conn = http.client.HTTPConnection(peer.host, peer.port, timeout=timeout)
        try:
            conn.request(method, path, body=data or None, headers=all_headers)
            res = conn.getresponse()
            raw = res.read(2_000_000)
        finally:
            conn.close()
        if trusted:
            # Whoever answers at this address must hold our pair key.
            if res.getheader("X-OT-Unpaired"):
                self._peer_unpaired(peer.id)
                raise IdentityMismatch(f"{trusted.name} removed this pairing.", unpaired=True)
            key = bytes.fromhex(trusted.key)
            if not verify_response(key, auth, res.status, raw, res.getheader("X-OT-Auth")):
                log.warning(
                    "An answer claiming to be %s wasn't signed by it", log_safe(trusted.name)
                )
                raise IdentityMismatch(f"The device at {peer.address} isn’t {trusted.name}.")
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

    def _upsert_peer(
        self, info: dict[str, Any], host: str, *, manual: bool = False, verified: bool = False
    ) -> Peer | None:
        """Note what a device says about itself.

        For a paired device, nothing changes unless ``verified`` (it proved the
        pair key at ``host``): otherwise anyone could copy its id into their own
        announcements and have its files sent to them, or rename it.
        """
        peer_id = info.get("id")
        port = info.get("port")
        if not valid_id(peer_id) or peer_id == self.id or not str(peer_id).startswith("d-"):
            return None
        if not isinstance(port, int) or not 1 <= port <= 65535:
            return None
        assert isinstance(peer_id, str)  # noqa: S101 - narrowed by valid_id
        trusted = self.trust.get(peer_id) is not None
        with self._lock:
            peer = self._peers.get(peer_id)
            if not verified and time.monotonic() - self._byes.get(peer_id, -BYE_QUIET) < BYE_QUIET:
                if not self._stop.is_set():  # back already? ask it
                    probe = peer or Peer(peer_id, "", "computer", "unknown", host, port)
                    self._executor.submit(self._hello, probe)
                return peer
            self._byes.pop(peer_id, None)
            if trusted and not verified:
                if peer is not None and (peer.host, peer.port) == (host, port):
                    peer.last_seen = time.monotonic()  # alive where we know it is
                    peer.pairing_at = time.monotonic() if info.get("pairing") else 0.0
                return peer
            is_new = peer is None or not peer.online
            if peer is None:
                peer = Peer(peer_id, "", "computer", "unknown", host, port)
                self._peers[peer_id] = peer
            moved = (peer.host, peer.port) != (host, port)
            peer.name = clean_name(info.get("name"))
            peer.form = clean_form(info.get("form"))
            peer.platform = clean_platform(info.get("platform"))
            peer.host, peer.port = host, port
            peer.version = str(info.get("version") or info.get("ver") or "")[:20]
            peer.accepts = "paired" if info.get("accepts") == "paired" else "everyone"
            peer.manual = peer.manual or manual
            peer.last_seen = time.monotonic()
            peer.pairing_at = time.monotonic() if info.get("pairing") else 0.0
            if isinstance(info.get("visitors"), list):
                peer.visitors = _clean_visitors(info["visitors"])
            if is_new or moved:
                log.info("Found %s (%s) at %s", log_safe(peer.name), peer.platform, peer.address)
        if trusted:
            self.trust.rename(peer_id, peer.name)
            self.trust.seen(peer_id, form=peer.form, platform=peer.platform)
        return peer

    def _on_packet(self, packet: dict[str, Any], src: str) -> None:
        if packet.get("id") == self.id or self._stop.is_set():
            return
        self._multicast_heard = time.monotonic()
        kind = packet["type"]
        if kind == "find":
            if self.pairing_open and self.discovery:
                # Answer straight back too: on some networks multicast only goes one way.
                answer = {"type": "reply", **self._describe()}
                self.discovery.send(answer)
                self.discovery.send_to(answer, src)
            return
        if kind == "bye":
            with self._lock:
                peer = self._peers.get(packet["id"])
                here = peer is not None and peer.host == src
                if peer and here:  # from where we know it is
                    self._gone(peer)
            if peer and not here:
                # Another of its addresses (two network interfaces), or someone
                # else's packet: ask it where we know it is.
                log.info("bye from %s for %s at %s; checking", src, log_safe(peer.name), peer.host)
                self._executor.submit(self._check_gone, peer)
            return
        peer_id = str(packet["id"])
        port = packet.get("port")
        if self.trust.get(peer_id) is not None:
            with self._lock:
                peer = self._peers.get(peer_id)
                here = peer is not None and (peer.host, peer.port) == (src, port)
            if here:
                self._upsert_peer(packet, src)  # liveness only
                if peer is not None and peer.rev != packet.get("rev"):
                    peer.rev = packet.get("rev", -1) if isinstance(packet.get("rev"), int) else -1
                    self._executor.submit(self._hello, peer)
            elif isinstance(port, int):
                self._verify_address_soon(peer_id, src, port)
            return
        with self._lock:
            known = self._peers.get(peer_id)
            fresh = known is None or not known.online
            changed = known is not None and known.rev != packet.get("rev")
        peer = self._upsert_peer(packet, src)
        if peer is None:
            return
        peer.rev = packet.get("rev", -1) if isinstance(packet.get("rev"), int) else -1
        if fresh:
            # Answer straight away, and say hello over HTTP so they learn about
            # us even if multicast only works in one direction.
            if kind == "announce" and self.discovery:
                self.discovery.reply_soon()
            self._executor.submit(self._hello, peer)
        elif changed:
            self._executor.submit(self._hello, peer)

    def _check_gone(self, peer: Peer) -> None:
        if not self._hello(peer):
            with self._lock:
                self._gone(peer)

    def _gone(self, peer: Peer) -> None:
        """It said goodbye. Packets it sent just before (UDP can reorder) or a
        hello still being handled must not bring it back: for a few seconds
        only our own hello to it does (see _upsert_peer)."""
        peer.last_seen = 0.0
        peer.visitors = []
        self._byes[peer.id] = time.monotonic()

    def _hello(self, peer: Peer) -> bool:
        """Say hello over HTTP. For a paired device, a valid (signed) answer also
        proves it is really at ``peer.host``."""
        try:
            status, info = self._http(
                peer, "POST", f"{P2P}/hello", body=self.self_info(include_visitors=False), timeout=3
            )
        except IdentityMismatch as exc:
            if not exc.unpaired:
                log.info("address_verification_failed: %s at %s", log_safe(peer.name), peer.address)
            return False
        except OSError:
            return False
        if status != 200 or info.get("id") != peer.id:
            return False
        self._upsert_peer(info, peer.host, manual=peer.manual, verified=True)
        return True

    def _verify_address_soon(self, peer_id: str, host: str, port: int) -> None:
        """A paired device seems to be at a new address: check before believing it."""
        key = (peer_id, host, port)
        now = time.monotonic()
        with self._lock:
            if now - self._verifying.get(key, -1e9) < VERIFY_EVERY:
                return
            self._verifying[key] = now
            trusted = self.trust.get(peer_id)
        if trusted is None:
            return
        candidate = Peer(peer_id, trusted.name, trusted.form, trusted.platform, host, port)
        self._executor.submit(self._hello, candidate)

    def handle_hello(
        self, info: dict[str, Any], host: str, *, signed: bool = False
    ) -> dict[str, Any]:
        """Another app says hello. ``signed``: it proved it's the paired device it claims."""
        if self._stop.is_set():  # closing: answering would show us online after our bye
            raise MeshError(503, "closing", f"{self.identity.name} is closing.")
        peer = self._upsert_peer(info, host, verified=signed)
        if (
            peer is None
            and self.trust.get(str(info.get("id", "")))
            and isinstance(info.get("port"), int)
        ):
            # A paired id at an unproven address: check it ourselves.
            self._verify_address_soon(str(info["id"]), host, int(info["port"]))
        return self.self_info()

    def connect(self, address: str) -> Peer:
        """Add an app by ``host:port`` (for networks where discovery is blocked)."""
        host, port = _parse_address(address)
        probe = Peer("d-00000000", "", "computer", "unknown", host, port)
        try:
            status, info = self._http(probe, "GET", f"{P2P}/info", timeout=4)
        except OSError as exc:
            raise MeshError(
                502,
                "device_unreachable",
                f"Couldn’t reach {host}:{port}. Check the address and that both devices are on the same Wi-Fi.",
            ) from exc
        if status != 200 or not valid_id(info.get("id")):
            raise MeshError(502, "device_unreachable", f"{host}:{port} isn’t an Open Transfer app.")
        if info.get("id") == self.id:
            raise MeshError(400, "invalid_request", "That’s this device.")
        if self.trust.get(str(info["id"])):
            # Paired: it has to prove it's really that device at this address.
            candidate = Peer(str(info["id"]), "", "computer", "unknown", host, port)
            if not self._hello(candidate) and self.trust.get(str(info["id"])):
                raise MeshError(
                    502, "identity_mismatch",
                    f"The device at {host}:{port} says it’s a device you paired with, but couldn’t prove it.",
                )  # fmt: skip
            peer = self._peers.get(str(info["id"])) or self._upsert_peer(info, host, manual=True)
        else:
            peer = self._upsert_peer(info, host, manual=True)
        if peer is None:
            raise MeshError(502, "device_unreachable", f"{host}:{port} isn’t an Open Transfer app.")
        peer.manual = True
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
    #
    # SRP over the 6-digit code; see docs/trust-model.md. "A" shows the code,
    # "B" enters (or scans) it. The code is only accepted while A's Add device
    # screen is open (the pairing window), and A's owner must press Allow.

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

    @property
    def pairing_open(self) -> bool:
        return time.monotonic() < self._pair_window_until

    def open_pairing(self) -> dict[str, Any]:
        """The owner opened (or still has open) the Add device screen."""
        was_open = self.pairing_open
        if not was_open:
            self._rotate_code()  # a fresh code each time the screen opens
        self._pair_window_until = time.monotonic() + PAIR_WINDOW
        if not was_open:
            self._bump()
        return self.pairing_view()

    def close_pairing(self) -> None:
        if self.pairing_open:
            self._pair_window_until = 0.0
            self._bump()

    def pairing_view(self) -> dict[str, Any]:
        ip = network.primary_ip()
        return {
            "code": self.pair_code,
            "expires_in": self.pair_code_expires_in(),
            "open": self.pairing_open,
            "address": f"{ip}:{self.port}" if ip else "",
        }

    def multicast_working(self) -> bool:
        """Have we heard another device's multicast lately?"""
        return bool(self.discovery and self.discovery.working) and (
            time.monotonic() - self._multicast_heard < 60
        )

    def check_visitor_code(self, code: str, client: str) -> bool | float:
        """``True`` if ``code`` is the current pairing code; seconds to wait if rate-limited."""
        wait = self._pair_limiter.attempt(f"visitor:{client}")
        if wait:
            return wait
        if self.pairing_open and secrets.compare_digest(str(code).strip(), self.pair_code):
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

    # A: the device showing the code

    def pair_begin(self, device: dict[str, Any], client: str) -> dict[str, Any]:
        """Step 1 on the device showing the code: start an SRP exchange."""
        device_id = device.get("id")
        if not valid_id(device_id) or device_id == self.id or not str(device_id).startswith("d-"):
            raise MeshError(400, "invalid_request", "Invalid pairing request.")
        if not self.pairing_open:
            raise MeshError(
                409, "pairing_not_open",
                f"{self.identity.name} isn’t showing a pairing code. Open Add device on it first.",
            )  # fmt: skip
        session = PairSession(
            id=secrets.token_urlsafe(12),
            device={k: device.get(k) for k in ("id", "name", "form", "platform", "port")},
            client=client,
            server=srp.Server(f"{self.id}|{device_id}", self.pair_code),
        )
        with self._lock:
            self._prune_pair_sessions()
            active = [
                p for p in self._pair_sessions.values() if p.state in {"proving", "confirming"}
            ]
            if sum(1 for p in active if p.client == client) >= 5:
                raise MeshError(
                    429,
                    "rate_limited",
                    "Too many pairing attempts. Try again in a minute.",
                    retry_after=60,
                )
            self._pair_sessions[session.id] = session
        return {
            "session": session.id,
            "salt": session.server.salt.hex(),
            "b": format(session.server.b_pub, "x"),
            "device": self.self_info(include_visitors=False),
        }

    def pair_prove(
        self, session_id: str, a_hex: str, proof_hex: str, client: str
    ) -> dict[str, Any]:
        """Step 2: check the other device's SRP proof, then ask our owner."""
        with self._lock:
            session = self._pair_sessions.get(session_id)
        if session is None or session.client != client or session.state != "proving":
            raise MeshError(409, "pairing_expired", "That pairing attempt has ended. Start again.")
        wait = self._pair_limiter.attempt(f"pair:{client}")
        if wait:
            session.state = "failed"  # else it counts as in progress in pair_begin for 3 min
            raise MeshError(
                429, "rate_limited", f"Too many wrong codes. Try again in {round(wait)} s.",
                retry_after=round(wait),
            )  # fmt: skip
        if not self.pairing_open:
            session.state = "expired"
            raise MeshError(
                409, "pairing_expired", f"{self.identity.name} closed its pairing screen."
            )
        try:
            server_proof = session.server.verify(int(a_hex, 16), bytes.fromhex(proof_hex))
        except (srp.SRPError, ValueError) as exc:
            session.state = "failed"
            self._note_pair_failure()
            raise MeshError(
                403, "pairing_code_invalid",
                f"That code isn’t the one {self.identity.name} is showing. Codes change every 10 minutes.",
            ) from exc  # fmt: skip
        self._pair_limiter.reset(f"pair:{client}")
        session.key = session.server.key or b""
        session.state, session.confirm_by = "confirming", time.monotonic() + PAIR_CONFIRM_TTL
        self._rotate_code()  # this code is used up
        self._emit("pair_request", self._pair_request_view(session))
        self._bump()
        return {"m2": server_proof.hex()}

    def pair_status(self, session_id: str, mac: str) -> dict[str, Any]:
        """Step 3, polled by the other device: has our owner allowed it?"""
        with self._lock:
            session = self._pair_sessions.get(session_id)
        if (
            session is None
            or not session.key
            or not secrets.compare_digest(session_mac(session.key, "status", session_id), str(mac))
        ):
            raise MeshError(404, "pairing_expired", "That pairing attempt has ended. Start again.")
        if session.state == "confirming" and time.monotonic() > session.confirm_by:
            session.state = "expired"
        return {
            "status": session.state,
            "mac": session_mac(session.key, "answer", session_id, session.state),
        }

    def decide_pairing(self, session_id: str, allow: bool) -> None:
        """Our owner answered "Pair with …?"."""
        with self._lock:
            session = self._pair_sessions.get(session_id)
        if (
            session is None
            or session.state != "confirming"
            or time.monotonic() > session.confirm_by
        ):
            raise MeshError(409, "pairing_expired", "That pairing request has ended.")
        device = session.device
        if not allow:
            session.state = "denied"
            self._bump()
            return
        key = derive_pair_key(session.key, self.id, str(device["id"]))
        self.trust.add(
            str(device["id"]), clean_name(device.get("name")), key,
            form=clean_form(device.get("form")), platform=clean_platform(device.get("platform")),
        )  # fmt: skip
        session.state = "allowed"
        if isinstance(device.get("port"), int):
            self._upsert_peer(device, session.client, verified=True)
        log.info("Paired with %s", log_safe(clean_name(device.get("name"))))
        self._bump()

    def _pair_request_view(self, session: PairSession) -> dict[str, Any]:
        d = session.device
        return {
            "id": session.id,
            "device": {
                "id": d.get("id"),
                "name": clean_name(d.get("name")),
                "form": clean_form(d.get("form")),
                "platform": clean_platform(d.get("platform")),
            },
            "expires_in": max(0, round(session.confirm_by - time.monotonic())),
        }

    def _prune_pair_sessions(self) -> None:
        now = time.monotonic()
        for sid, s in list(self._pair_sessions.items()):
            if now - s.created > PAIR_SESSION_TTL + PAIR_CONFIRM_TTL:
                del self._pair_sessions[sid]

    # B: the device entering the code

    def pair_with(
        self, code: str, address: str | None = None, device_id: str | None = None
    ) -> Peer:
        """Pair with the device showing ``code``: the chosen one, the one at
        ``address``, or whichever nearby device has its pairing window open."""
        code = "".join(ch for ch in str(code) if ch.isdigit())
        if len(code) != 6:
            raise MeshError(
                400, "pairing_code_invalid", "Enter the 6-digit code shown on the other device."
            )
        if address:
            candidates = [self.connect(address)]
        elif device_id:
            with self._lock:
                peer = self._peers.get(device_id)
            if peer is None or not peer.online:
                raise MeshError(
                    404,
                    "device_unreachable",
                    "That device isn’t reachable right now. Check it’s nearby with Open Transfer open.",
                )
            candidates = [peer]
        else:
            candidates = self._pairing_candidates()
        chosen = bool(address or device_id)  # the user picked it: tell them if it's paired already
        if chosen and self.trust.get(candidates[0].id) and self._hello(candidates[0]):
            raise MeshError(
                409, "already_paired", f"You’re already paired with {candidates[0].name}."
            )
        failures: list[MeshError] = []
        for peer in candidates:
            try:
                return self._pair_srp(peer, code)
            except MeshError as exc:
                if exc.code in {"rate_limited", "trust_rejected"}:
                    raise
                failures.append(exc)
        wrong = [f for f in failures if f.code == "pairing_code_invalid"]
        raise (wrong[0] if wrong else failures[0])

    def _pairing_candidates(self) -> list[Peer]:
        """Devices that may be showing a code: never just the ones multicast found.

        Every reachable device is asked whether its pairing window is open (an
        earlier answer may be stale), and devices we're already paired with are
        left out: entering a code is for a new device.
        """
        if self.discovery and self.discovery.working:
            self.discovery.find()
        with self._lock:
            online = [p for p in self._peers.values() if p.online]
        askers = [
            threading.Thread(target=self._refresh_info, args=(p,), daemon=True) for p in online
        ]
        for t in askers:
            t.start()
        deadline = time.monotonic() + 2.5
        for t in askers:
            t.join(max(0.0, deadline - time.monotonic()))
        time.sleep(max(0.0, min(0.6, deadline - time.monotonic())))  # multicast answers
        with self._lock:
            reachable = [p for p in self._peers.values() if p.online]
            found = [p for p in reachable if p.pairing and not self.trust.get(p.id)]
        if found:
            return found
        if not reachable:
            raise MeshError(
                404, "discovery_unavailable",
                "No nearby devices found automatically — this network may block it. "
                "Scan the QR code on the other device, or enter the address it shows.",
            )  # fmt: skip
        raise MeshError(
            404, "pairing_not_open",
            "None of the nearby devices you aren’t paired with is showing a pairing code. "
            "Open Add device on the other device first.",
        )  # fmt: skip

    def _refresh_info(self, peer: Peer) -> None:
        with contextlib.suppress(OSError):
            status, info = self._http(peer, "GET", f"{P2P}/info", timeout=2)
            if status == 200 and info.get("id") == peer.id:
                self._upsert_peer(info, peer.host, verified=self.trust.get(peer.id) is not None)

    def _pair_srp(self, peer: Peer, code: str) -> Peer:
        target = dataclasses.replace(peer, visitors=[])  # one address for every step
        client = srp.Client(f"{peer.id}|{self.id}", code)

        def post(path: str, body: dict[str, Any], timeout: float = 8) -> dict[str, Any]:
            try:
                status, data = self._unsigned_http(
                    target, "POST", f"{P2P}/pair/{path}", body, timeout
                )
            except OSError as exc:
                raise MeshError(
                    502, "device_unreachable", f"Lost connection to {peer.name}."
                ) from exc
            if status != 200:
                raw_error = data.get("error")
                error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
                code_ = str(error.get("code") or "pairing_failed")
                extra = (
                    {"retry_after": error.get("retry_after")} if error.get("retry_after") else {}
                )
                raise MeshError(status, code_, _remote_message(data, "Pairing failed."), **extra)
            return data

        begun = post("begin", {"device": self.self_info(include_visitors=False)})
        if (begun.get("device") or {}).get("id") != peer.id:
            raise MeshError(
                502, "identity_mismatch", f"The device at {peer.address} isn’t {peer.name}."
            )
        session = str(begun.get("session", ""))
        try:
            proof = client.respond(bytes.fromhex(str(begun["salt"])), int(str(begun["b"]), 16))
        except (KeyError, ValueError, srp.SRPError) as exc:
            raise MeshError(
                502, "identity_mismatch", f"{peer.name} sent an invalid pairing answer."
            ) from exc
        proved = post(
            "prove", {"session": session, "a": format(client.a_pub, "x"), "m1": proof.hex()}
        )
        try:
            client.check(bytes.fromhex(str(proved.get("m2", ""))))
        except (ValueError, srp.SRPError) as exc:
            raise MeshError(
                502, "identity_mismatch", f"{peer.name} couldn’t prove it’s showing that code."
            ) from exc
        key = client.key or b""
        deadline = time.monotonic() + PAIR_CONFIRM_TTL + 5
        while True:
            answer = post(
                "status", {"session": session, "mac": session_mac(key, "status", session)}
            )
            state = str(answer.get("status", ""))
            if not secrets.compare_digest(
                session_mac(key, "answer", session, state), str(answer.get("mac", ""))
            ):
                raise MeshError(
                    502, "identity_mismatch", f"{peer.name}’s answer couldn’t be verified."
                )
            if state == "allowed":
                break
            if state in {"denied", "expired"} or time.monotonic() > deadline:
                raise MeshError(
                    403, "trust_rejected",
                    f"{peer.name} didn’t allow the pairing." if state == "denied"
                    else f"Nobody allowed the pairing on {peer.name} in time.",
                )  # fmt: skip
            time.sleep(1.0)
        self.trust.add(
            peer.id, peer.name, derive_pair_key(key, peer.id, self.id),
            form=peer.form, platform=peer.platform,
        )  # fmt: skip
        self._upsert_peer(begun["device"], target.host, verified=True)
        log.info("Paired with %s", log_safe(peer.name))
        return self._peers.get(peer.id, peer)

    def _unsigned_http(
        self, peer: Peer, method: str, path: str, body: dict[str, Any], timeout: float
    ) -> tuple[int, dict[str, Any]]:
        """Pairing messages are authenticated by SRP itself, not the pair key."""
        stranger = dataclasses.replace(peer, id="d-00000000")
        return self._http(stranger, method, path, body=body, timeout=timeout)

    # Unpairing

    def unpair(self, device_id: str) -> bool:
        """Forget the pair key here and, if it's reachable, on the other device too."""
        trusted = self.trust.get(device_id)
        if trusted is None:
            return False
        with self._lock:
            peer = self._peers.get(device_id)
        if peer is not None and peer.online:
            with contextlib.suppress(OSError):
                self._http(peer, "DELETE", f"{P2P}/pair", timeout=4)
        removed = self.trust.remove(device_id)
        log.info("Unpaired %s", log_safe(trusted.name))
        self._bump()
        return removed

    def unpaired_by(self, device_id: str) -> None:
        """The other device unpaired us (a signed request)."""
        if self.trust.remove(device_id):
            log.info("A device removed its pairing with this one")
            self._bump()

    def _peer_unpaired(self, device_id: str) -> None:
        """A device answered that it no longer trusts us: drop our side too.

        Not within a minute of pairing: the device that shows the code stores the
        key first and the other a moment later, so a hello in between would
        otherwise undo a pairing that is still completing.
        """
        trusted = self.trust.get(device_id)
        if trusted and time.time() - trusted.paired_at < PAIR_CONFIRM_TTL:
            return
        if trusted and self.trust.remove(device_id):
            log.info("%s removed this pairing", log_safe(trusted.name))
            self._emit("unpaired", {"id": device_id, "name": trusted.name})
            self._bump()

    def forget(self, device_id: str) -> bool:
        """Remove a device from the list: unpair it and drop what we know about it."""
        unpaired = self.unpair(device_id)
        with self._lock:
            dropped = self._peers.pop(device_id, None) is not None
        self._bump()
        return unpaired or dropped

    # ----------------------------------------------------------- lifecycle
    #
    # Every state change goes through _move_session (receiver) or _move_delivery
    # (sender), which refuse transitions lifecycle.can_move doesn't allow, so a
    # final state is never overwritten by a late or duplicate message.
    # Authority: the receiver decides accepted, file completed and the final
    # state of what it received; the sender asks it (_settle) before recording
    # a failure it can't prove on its own.

    def _move_session(
        self, session: IncomingSession, state: str, code: str = "", detail: str = ""
    ) -> bool:
        """Change an incoming session's state. Call with ``self._lock`` held."""
        if not can_move(lifecycle_state(session.state), lifecycle_state(state)):
            if state != session.state:
                log.debug("ignored %s -> %s for %s", session.state, state, session.id)
            return False
        now = time.monotonic()
        if state == ACCEPTED and session.state != ACCEPTED:
            session.deadline = now + ACCEPT_TTL
        if state == RECEIVING:
            session.last_activity = now
            session.started_at = session.started_at or time.time()
        session.state = state
        if code or detail:
            info = describe(code, detail)
            session.reason_code, session.reason = info["reason_code"], info["reason"]
        if state in FINAL:
            session.finished = now
            session.finished_at = time.time()
        return True

    def _move_delivery(
        self, delivery: Delivery, state: str, code: str = "", detail: str = ""
    ) -> bool:
        """Change the state of a delivery to another app or a visitor of another app."""
        with self._lock:
            if not can_move(lifecycle_state(delivery.view_state()), lifecycle_state(state)):
                return False
            now = time.monotonic()
            if state == ACCEPTED and delivery.state != ACCEPTED:
                delivery.deadline = now + ACCEPT_TTL
            if state == "sending":
                delivery.last_activity = now
                delivery.started_at = delivery.started_at or time.time()
            if state in FINAL:
                delivery.finished_at = time.time()
            delivery.state = state
            if code or detail:
                info = describe(code, detail)
                delivery.reason_code, delivery.reason = info["reason_code"], info["reason"]
            return True

    def _finish_session(self, session: IncomingSession) -> None:
        """Once every file has an outcome: completed, partial or failed. Lock held."""
        files = session.files
        if session.state in FINAL or not all(f.state in {DONE, FAILED} for f in files):
            return
        failed = [f for f in files if f.state == FAILED]
        if not failed:
            self._move_session(session, DONE)
        elif len(failed) == len(files):
            self._move_session(session, FAILED, failed[0].reason_code or "unknown_failure")
        else:
            arrived = len(files) - len(failed)
            self._move_session(
                session, PARTIAL, failed[0].reason_code or "unknown_failure",
                f"{arrived} of {len(files)} files arrived. "
                + describe(failed[0].reason_code or "unknown_failure")["reason"],
            )  # fmt: skip

    def _finish_delivery(self, job: Job, delivery: Delivery) -> None:
        """Once every file has an outcome for this receiver: completed, partial or failed."""
        if delivery.session is not None:
            return  # a local receiver's session decides
        total = len(job.files)
        done, failed = delivery.files_done, delivery.files_failed
        if len(done | set(failed)) < total:
            return
        if not failed:
            self._move_delivery(delivery, DONE)
            return
        code = next(iter(failed.values())) or "unknown_failure"
        if not done:
            self._move_delivery(delivery, FAILED, code)
        else:
            self._move_delivery(
                delivery, PARTIAL, code,
                f"{len(done)} of {total} files arrived. " + describe(code)["reason"],
            )  # fmt: skip

    def _remote_status(self, delivery: Delivery) -> dict[str, Any] | None:
        """Ask the receiving app how this transfer went, or ``None`` if it can't say."""
        peer = delivery.peer
        if peer is None or not delivery.remote_id:
            return None
        for attempt in range(RECONCILE_TRIES):
            try:
                status, data = self._http(
                    peer, "GET", f"{P2P}/offers/{delivery.remote_id}",
                    headers={"X-OT-Secret": delivery.remote_secret}, timeout=5,
                )  # fmt: skip
            except OSError:
                if attempt + 1 < RECONCILE_TRIES and not self._stop.is_set():
                    time.sleep(1.0)
                continue
            return data if status == 200 else None
        return None

    def _adopt(self, job: Job, delivery: Delivery, status: dict[str, Any]) -> bool:
        """Take the receiver's word for what arrived; True if the delivery is now final."""
        files = status.get("files")
        if isinstance(files, list):
            for i, f in enumerate(files[: len(job.files)]):
                if not isinstance(f, dict):
                    continue
                if f.get("state") == DONE:
                    delivery.files_done.add(i)
                    delivery.files_failed.pop(i, None)
                elif f.get("state") == FAILED and i not in delivery.files_done:
                    delivery.files_failed[i] = str(f.get("reason_code") or "unknown_failure")
        state = str(status.get("state") or "")
        code = str(status.get("reason_code") or "")
        detail = str(status.get("reason") or "")
        if state in {DONE, PARTIAL, FAILED, DECLINED, EXPIRED, CANCELED}:
            fallback = {
                DECLINED: "receiver_declined",
                EXPIRED: "expired",
                CANCELED: "receiver_cancelled",
            }
            if state == FAILED and not code:
                code = "unknown_failure"
            self._move_delivery(delivery, state, code or fallback.get(state, ""), detail)
        elif state in {ACCEPTED, RECEIVING} and delivery.state in {"offering", "waiting"}:
            self._move_delivery(delivery, ACCEPTED)
        elif state == PENDING and delivery.state == "offering":
            self._move_delivery(delivery, "waiting")
        return delivery.view_state() in FINAL

    def _settle(self, job: Job, delivery: Delivery, code: str, detail: str = "") -> None:
        """Give up on a delivery, unless the receiver can show it got further than we know."""
        status = self._remote_status(delivery)
        if status is not None and self._adopt(job, delivery, status):
            self._touch_job(job)
            return
        stage = delivery.view_state()
        if status is None and code not in {"expired", "sender_disconnected", "app_closed"}:
            code, detail = "receiver_disconnected", detail
        if stage in {"offering", "waiting"}:
            self._move_delivery(delivery, EXPIRED if code == "expired" else FAILED, code, detail)
        else:
            for i in range(len(job.files)):
                if i not in delivery.files_done:
                    delivery.files_failed.setdefault(i, code)
            self._finish_delivery(job, delivery)
            if delivery.view_state() not in FINAL:  # nothing was tracked per file
                self._move_delivery(delivery, FAILED, code, detail)
        self._touch_job(job)

    def _touch_job(self, job: Job) -> None:
        """Note when every delivery has ended, and save the job to history."""
        if not job.finished and all(d.view_state() in FINAL for d in job.deliveries.values()):
            job.finished = time.monotonic()
        self._record_job(job)

    def _check_deadlines(self, now: float | None = None) -> None:
        """End every transfer that has waited too long in a non-final state.

        Called by housekeeping every 2 s; tests call it with a ``now`` in the future.
        """
        now = time.monotonic() if now is None else now
        sessions: list[IncomingSession] = []
        due: list[tuple[Job, Delivery, str]] = []
        with self._lock:
            for s in self._incoming.values():
                changed = False
                if s.state == PENDING and now - s.created > OFFER_TTL:
                    changed = self._move_session(s, EXPIRED, "expired")
                elif s.state == ACCEPTED and s.deadline and now > s.deadline:
                    changed = self._move_session(s, FAILED, "sender_disconnected")
                elif s.state == RECEIVING and now - s.last_activity > STALL_TIMEOUT:
                    for f in s.files:
                        if f.state not in {DONE, FAILED}:
                            f.state, f.reason_code = FAILED, "transfer_stalled"
                            f.error = describe("transfer_stalled")["reason"]
                    self._finish_session(s)
                    changed = True
                if changed:
                    sessions.append(s)
            for job in self._jobs.values():
                for d in job.deliveries.values():
                    if d.session is not None or d.busy or d.view_state() in FINAL:
                        continue
                    if d.state in {"offering", "waiting"}:
                        if now - job.created > OFFER_TTL + SENDER_GRACE:
                            due.append((job, d, "expired"))
                    elif d.state == ACCEPTED:
                        if d.deadline and now > d.deadline:
                            due.append((job, d, "sender_disconnected"))
                    elif d.state == "sending" and now - d.last_activity > STALL_TIMEOUT:
                        due.append((job, d, "transfer_stalled"))
        for s in sessions:
            self._record_session(s)
        for job, d, code in due:
            self._settle(job, d, code)

    def _status_view(self, session: IncomingSession) -> dict[str, Any]:
        return {
            "id": session.id,
            "state": session.state,
            "reason": session.reason,
            "reason_code": session.reason_code,
            "files": [
                {"state": f.state, "received": f.received, "reason_code": f.reason_code}
                for f in session.files
            ],
        }

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
        problem = self._space_problem(session)
        if problem:
            self._move_session(session, DECLINED, problem)
        elif owner_target and (self.config.auto_accept or paired):
            self._accept(session)
        with self._lock:
            self._incoming[session.id] = session
        self._record_session(session)
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
        """The reason code that rules this offer out, or ``""``."""
        limit = self.config.max_upload_size
        if limit and any(f.size > limit for f in session.files):
            return "file_too_large"
        store = self.storage if session.target == self.id else self.inbox(session.target)
        if session.total > store.usage()["free"]:
            return "insufficient_storage"
        return ""

    def _accept(self, session: IncomingSession) -> None:
        self._move_session(session, ACCEPTED)
        for f in session.files:
            f.state = "waiting"

    def decide(self, session_id: str, viewer: Viewer, accept: bool) -> IncomingSession:
        session = self._session_for(session_id, viewer)
        with self._lock:
            if session.state != PENDING:
                raise MeshError(409, "already_decided", "This transfer was already answered.")
            problem = self._space_problem(session) if accept else ""
            if problem:
                # Say why to both sides instead of leaving the offer waiting forever.
                self._move_session(session, DECLINED, problem)
            elif accept:
                self._accept(session)
            else:
                self._move_session(session, DECLINED, "receiver_declined")
        self._record_session(session)
        if problem:
            status = 507 if problem == "insufficient_storage" else 413
            raise MeshError(status, problem, session.reason)
        return session

    def cancel_incoming(
        self, session_id: str, viewer: Viewer | None, *, secret: str = "", why: str = ""
    ) -> None:
        """Stop receiving: by the receiver (``viewer``) or the sender (``secret``).

        ``why == "app_closed"``: the sender's app is shutting down, which is a
        failure (send again), not a decision to cancel.
        """
        if viewer is not None:
            session = self._session_for(session_id, viewer)
        else:
            session = self._session_by_secret(session_id, secret)
        with self._lock:
            if viewer is not None:
                self._move_session(session, CANCELED, "receiver_cancelled")
            elif why == "app_closed":
                self._move_session(
                    session, FAILED, "sender_disconnected", "The sender closed Open Transfer."
                )
            else:
                self._move_session(session, CANCELED, "sender_cancelled")
        self._record_session(session)

    def offer_status(self, session_id: str, secret: str) -> dict[str, Any]:
        """For the sender: where its offer stands (answered for a while after it ends)."""
        with self._lock:
            kept = self._finished_status.get(session_id)
        if kept is not None and session_id not in self._incoming:
            if not secrets.compare_digest(kept[0], secret or ""):
                raise MeshError(404, "not_found", "Unknown transfer.")
            return kept[1]
        return self._status_view(self._session_by_secret(session_id, secret))

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
            if session.state in FINAL:
                raise MeshError(
                    410,
                    session.reason_code or session.state,
                    session.reason or "This transfer has ended.",
                )
            if session.state not in {ACCEPTED, RECEIVING}:
                raise MeshError(409, "not_accepted", "The receiver hasn’t accepted yet.")
            if not 0 <= index < len(session.files):
                raise MeshError(404, "not_found", "No such file in this transfer.")
            item = session.files[index]
            if item.state in {RECEIVING, DONE, FAILED}:
                raise MeshError(409, "duplicate", "This file was already sent.")
            if length is None:
                raise MeshError(411, "length_required", "Send a Content-Length header.")
            if length != item.size:
                raise MeshError(400, "size_mismatch", "The file size doesn’t match the offer.")
            item.state, item.received, item.error = RECEIVING, 0, ""
            self._move_session(session, RECEIVING)
        self._record_session(session)
        store = self.storage if session.target == self.id else self.inbox(session.target)

        def count(n: int) -> None:
            item.received += n
            if n:
                session.last_activity = time.monotonic()

        def stopped() -> bool:  # canceled, or given up on by the stall deadline
            return session.state in FINAL or item.state == FAILED

        reader = _Watched(stream, count, stopped)
        try:
            saved = store.save_stream(
                item.name, reader, length=length, max_size=self.config.max_upload_size
            )
        except BaseException as exc:  # record every failure; nothing may stay "receiving"
            with self._lock:
                if item.state != FAILED:
                    item.state, item.reason_code = FAILED, _receive_error_code(exc)
                    item.error = str(exc) if isinstance(exc, StorageError) else "Interrupted"
                self._finish_session(session)
            self._record_session(session)
            if session.state == CANCELED:
                raise MeshError(410, session.reason_code or CANCELED, session.reason) from exc
            if isinstance(exc, StorageError):
                raise
            if isinstance(exc, Exception):
                raise IncompleteUpload("The transfer was interrupted before it finished.") from exc
            raise
        with self._lock:
            item.state, item.saved_name, item.received = DONE, saved.name, item.size
            self._finish_session(session)
        self._record_session(session)
        log.info(
            "Received %s (%s bytes) from %s", log_safe(saved.name), saved.size,
            log_safe(session.origin.get("name")),
        )  # fmt: skip
        if session.target == self.id:
            self._emit("received", {"path": str(store.root / saved.name), "name": saved.name})
        return saved

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
                self._executor.submit(self._offer_remote_and_record, job, target_id, delivery)
        self._record_job(job)
        return job

    def _offer_remote_and_record(self, job: Job, target_id: str, delivery: Delivery) -> None:
        try:
            self._offer_remote(job, target_id, delivery)
        finally:
            self._record_job(job)

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
                    delivery.state = FAILED
                    delivery.reason_code = "receiver_unreachable"
                    delivery.reason = f"{peer.name} isn’t nearby."
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
            state, code = OFFER_ERRORS.get(exc.code, (FAILED, "unknown_failure"))
            self._move_delivery(delivery, state, code, str(exc))

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
            self._move_delivery(
                delivery, FAILED, "receiver_unreachable", f"Couldn’t reach {peer.name}."
            )
            return
        if status != 201:
            error = data.get("error")
            error_code = str(error.get("code", "")) if isinstance(error, dict) else ""
            state, code = OFFER_ERRORS.get(error_code, (FAILED, "unknown_failure"))
            if status == 403 and code == "unknown_failure":
                state, code = DECLINED, "permission_denied"
            self._move_delivery(
                delivery, state, code, _remote_message(data, f"{peer.name} refused the transfer.")
            )
            return
        delivery.remote_id = str(data.get("id", ""))
        delivery.remote_secret = str(data.get("secret", ""))
        if job.canceled or delivery.canceled:
            # Canceled (or the app closed) while the offer was on its way:
            # withdraw it now we know its id, saying why.
            self._move_delivery(delivery, CANCELED, "sender_cancelled")
            why = delivery.reason_code or "sender_cancelled"
            with contextlib.suppress(OSError):
                self._http(
                    peer,
                    "DELETE",
                    f"{P2P}/offers/{delivery.remote_id}",
                    headers={"X-OT-Secret": delivery.remote_secret, "X-OT-Reason": why},
                )
            return
        self._follow_remote(job, delivery, data)

    def _follow_remote(self, job: Job, delivery: Delivery, data: dict[str, Any]) -> None:
        """Watch an offer until it is answered (the deadline itself is in _check_deadlines)."""
        peer = delivery.peer
        assert peer is not None  # noqa: S101
        errors = 0
        while True:
            if self._adopt(job, delivery, data) or delivery.view_state() not in {
                "offering",
                "waiting",
            }:
                return  # answered (or settled by the deadline meanwhile)
            if job.canceled or delivery.canceled or self._stop.is_set():
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
                    self._settle(
                        job, delivery, "receiver_disconnected", "The receiver lost the transfer."
                    )
                    return
                errors = 0
            except OSError:
                errors += 1
                if errors >= 5:
                    self._settle(
                        job, delivery, "receiver_disconnected", f"Lost connection to {peer.name}."
                    )
                    return
                data = {}

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
            if delivery.view_state() in {ACCEPTED, "sending"}
            and index not in delivery.files_done
            and index not in delivery.files_failed
            and not delivery.direct
            and not delivery.canceled
        ]
        if not sinks:
            if any(d.view_state() in {"offering", "waiting"} for d in job.deliveries.values()):
                raise MeshError(409, "waiting", "Still waiting for the receivers to accept.")
            raise MeshError(409, "no_receivers", "Nobody accepted this transfer.")
        job.started_at = job.started_at or time.time()
        for sink in sinks:
            sink.start()
        received = 0
        try:
            while True:
                chunk = stream.read(CHUNK)
                if not chunk:
                    break
                received += len(chunk)
                live = []
                for sink in sinks:
                    if sink.alive:
                        live.append(sink)
                    elif sink.delivery.canceled:  # that one receiver was canceled: stop its stream
                        sink.abort("Canceled", "sender_cancelled")
                if not live or job.canceled:
                    break
                for sink in live:
                    sink.feed(chunk)
        except Exception:
            for sink in sinks:
                sink.abort("The sender stopped the transfer.")
            raise
        if received != length or job.canceled:
            why = "sender_cancelled" if job.canceled else "sender_disconnected"
            for sink in sinks:
                sink.abort(describe(why)["reason"], why)
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
            delivery = sink.delivery
            ok = sink.ok
            if delivery.session is None and not ok and not delivery.canceled:
                # The receiver may have the whole file even though its reply got
                # lost: ask before recording a failure.
                status = self._remote_status(delivery)
                if status is not None:
                    self._adopt(job, delivery, status)
                ok = index in delivery.files_done
                if not ok and delivery.view_state() not in FINAL:
                    delivery.files_failed.setdefault(
                        index, "receiver_disconnected" if status is None else sink.code
                    )
            if ok:
                delivery.files_done.add(index)
                delivery.files_failed.pop(index, None)
            self._finish_delivery(job, delivery)
            results[sink.target_id] = {"ok": ok, "error": "" if ok else sink.error}
        self._touch_job(job)
        return {"targets": results}

    def cancel_job(
        self, job_id: str, viewer_id: str, *, quiet: bool = False, code: str = "sender_cancelled"
    ) -> list[threading.Thread]:
        """Stop sending to everyone; returns the threads telling the receivers."""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.owner != viewer_id:
            if quiet:
                return []
            raise MeshError(404, "not_found", "That transfer is no longer available.")
        job.canceled = True
        tellers = [t for d in job.deliveries.values() if (t := self._cancel_delivery(job, d, code))]
        self._touch_job(job)
        return tellers

    def cancel_target(self, job_id: str, viewer: Viewer, target_id: str) -> None:
        job = self.job_for(job_id, viewer)
        delivery = job.deliveries.get(target_id)
        if delivery is None:
            raise MeshError(404, "not_found", "That device isn’t part of this transfer.")
        self._cancel_delivery(job, delivery)
        self._touch_job(job)

    def _cancel_delivery(
        self, job: Job, delivery: Delivery, code: str = "sender_cancelled"
    ) -> threading.Thread | None:
        """Stop one delivery and tell the receiver (on a thread, which is returned).

        ``code`` is ``sender_cancelled``, or ``app_closed`` when this app is
        shutting down (a failure to send again, not a decision).
        """
        if delivery.view_state() in FINAL:
            return None
        delivery.canceled = True  # stops feeding it
        state = FAILED if code == "app_closed" else CANCELED
        if delivery.session is not None:
            with contextlib.suppress(MeshError):
                self.cancel_incoming(
                    delivery.session.id, None, secret=delivery.session.secret, why=code
                )
            return None
        if delivery.peer is None or not delivery.remote_id:
            self._move_delivery(delivery, state, code)  # the offer itself withdraws on arrival
            return None
        receiving = delivery.view_state() == "sending" and code != "app_closed"
        if not receiving:
            self._move_delivery(delivery, state, code)
        peer, remote_id, secret = delivery.peer, delivery.remote_id, delivery.remote_secret

        def tell() -> None:
            with contextlib.suppress(OSError):
                self._http(
                    peer, "DELETE", f"{P2P}/offers/{remote_id}",
                    headers={"X-OT-Secret": secret, "X-OT-Reason": code}, timeout=3,
                )  # fmt: skip
            if receiving:
                # The last bytes may have landed just before the cancel: if the
                # receiver finished, it was delivered after all.
                status = self._remote_status(delivery)
                if status is None or not self._adopt(job, delivery, status):
                    self._move_delivery(delivery, state, code)
                self._touch_job(job)

        thread = threading.Thread(target=tell, name="ot-cancel", daemon=True)
        thread.start()
        return thread

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
            delivery.last_activity = time.monotonic()
            job.started_at = job.started_at or time.time()
        elif state == "failed":
            delivery.direct = False  # the files go the usual way instead
        elif state == "progress":
            delivery.sent = max(delivery.sent, int(sent))
            delivery.last_activity = time.monotonic()
        elif state == "done":
            delivery.sent = sum(int(f["size"]) for f in job.files)
            delivery.files_done = set(range(len(job.files)))
            self._finish_delivery(job, delivery)
        else:
            raise MeshError(400, "bad_request", "Unknown state.")
        self._touch_job(job)

    def direct_received(
        self, session_id: str, viewer: Viewer, index: int, received: int, done: bool
    ) -> None:
        """The receiving page reports a file arriving straight from the sender's browser."""
        session = self._session_for(session_id, viewer)
        with self._lock:
            if session.state not in {ACCEPTED, RECEIVING}:
                raise MeshError(
                    410,
                    session.reason_code or session.state,
                    session.reason or "This transfer has ended.",
                )
            if not 0 <= index < len(session.files):
                raise MeshError(404, "not_found", "No such file in this transfer.")
            item = session.files[index]
            if item.state in {DONE, FAILED}:
                return  # a late or repeated report
            item.received = max(0, min(item.size, int(received)))
            self._move_session(session, RECEIVING)
            if done:
                item.state, item.received, item.direct = DONE, item.size, True
                item.saved_name = item.name
                self._finish_session(session)
        self._record_session(session)

    # ------------------------------------------------------------- history
    #
    # The owner's transfers are recorded in ``History``. Each live session/job is
    # turned into a full snapshot (from the same views the UI shows) and saved
    # when it differs from the last one saved: right away at the transitions
    # that matter, and by housekeeping every couple of seconds for the rest.

    def _record_session(self, session: IncomingSession) -> None:
        """Save what the owner is receiving (visitors' inboxes aren't recorded)."""
        if session.target != self.id:
            return
        view = self._incoming_view(session)
        state = lifecycle_state(session.state)
        ident = self.identity
        record = TransferRecord(
            transfer_id=session.job or session.id,
            direction="received",
            peer_node=session.sender_node,
            sender=view["from"],
            files=[
                FileRecord(
                    name=f.name,
                    size=f.size,
                    mime=f.mime,
                    saved_name="" if f.direct else (f.saved_name or ""),
                    state=lifecycle_state(f.state),
                    error=f.error,
                )
                for f in session.files
            ],
            recipients=[
                RecipientRecord(
                    device_id=ident.id,
                    device={"name": ident.name, "form": ident.form, "platform": ident.platform},
                    state=state,
                    reason_code=session.reason_code,
                    reason=session.reason,
                    session_id=session.id,
                    bytes_done=session.received,
                    files_done=[i for i, f in enumerate(session.files) if f.state == DONE],
                    files_failed={
                        i: f.reason_code for i, f in enumerate(session.files) if f.state == FAILED
                    },
                    started_at=session.started_at,
                    finished_at=session.finished_at,
                )
            ],
            created_at=session.created_at,
            started_at=session.started_at,
            reason_code=session.reason_code,
            reason=session.reason,
        )
        self._save_record(f"s:{session.id}", record)

    def _record_job(self, job: Job) -> None:
        """Save what the owner is sending (a group send is one record)."""
        if job.owner != self.id:
            return
        view = self._job_view(job)
        recipients = []
        for target in view["targets"]:
            delivery = job.deliveries[target["id"]]
            local = delivery.session
            state = lifecycle_state(target["state"])
            recipients.append(
                RecipientRecord(
                    device_id=target["id"],
                    device={
                        k: target.get(k) for k in ("name", "form", "platform", "kind", "via_name")
                    },
                    state=state,
                    reason_code=target["reason_code"],
                    reason=target["reason"],
                    session_id=local.id if local else delivery.remote_id,
                    bytes_done=target["sent"],
                    files_done=target["files_done"],
                    files_failed=target["files_failed"],
                    started_at=local.started_at if local else delivery.started_at,
                    finished_at=local.finished_at if local else delivery.finished_at,
                )
            )
        failed = [r for r in recipients if r.reason_code and r.state != "completed"]
        record = TransferRecord(
            transfer_id=job.id,
            direction="sent",
            reason_code=failed[0].reason_code if failed else "",
            reason=failed[0].reason if failed else "",
            sender=dict(job.origin),
            files=[
                FileRecord(name=str(f["name"]), size=int(f["size"]), mime=str(f.get("mime", "")))
                for f in job.files
            ],
            recipients=recipients,
            created_at=job.created_at,
            started_at=job.started_at,
        )
        self._save_record(f"j:{job.id}", record)

    def _save_record(self, key: str, record: TransferRecord) -> None:
        snapshot = (
            record.state,
            tuple(
                (r.state, r.reason, r.bytes_done, tuple(r.files_done)) for r in record.recipients
            ),
            tuple((f.state, f.saved_name) for f in record.files),
        )
        with self._lock:
            if self._recorded.get(key) == snapshot:
                return
            self._recorded[key] = snapshot
        try:
            self.history.save(record)
        except Exception:  # history must never break a transfer
            log.warning("Couldn't save transfer history", exc_info=True)
            with self._lock:
                self._recorded.pop(key, None)

    def _record_all(self) -> None:
        with self._lock:
            sessions = list(self._incoming.values())
            jobs = list(self._jobs.values())
        for session in sessions:
            self._record_session(session)
        for job in jobs:
            self._record_job(job)

    def history_for(self, **filters: Any) -> list[dict[str, Any]]:
        """History records, newest first, each file marked with whether it still exists."""
        items = self.history.find(**filters)
        for item in items:
            for f in item["files"]:
                f["exists"] = None
                if item["direction"] == "received" and f["saved_name"]:
                    try:
                        self.storage.resolve(f["saved_name"])
                        f["exists"] = True
                    except StorageError:
                        f["exists"] = False
        return items

    # -------------------------------------------------------- housekeeping

    def _housekeeping(self) -> None:
        last_hello = 0.0
        last_prune = time.monotonic()
        manual_known: dict[str, float] = {}
        while not self._stop.wait(2.0):
            now = time.monotonic()
            self._record_all()  # catches progress and remote/sink/direct transitions
            if now - last_prune >= HISTORY_PRUNE_EVERY:
                last_prune = now
                with contextlib.suppress(Exception):
                    self.history.prune()
            self._check_deadlines(now)
            with self._lock:
                self._prune_pair_sessions()
                for attempt in [k for k, at in self._verifying.items() if now - at > 60]:
                    del self._verifying[attempt]
                for key in [
                    k for k, s in self._incoming.items()
                    if s.state in FINAL and now - s.finished > FINISHED_KEEP
                ]:  # fmt: skip
                    session = self._incoming.pop(key)
                    self._finished_status[key] = (session.secret, self._status_view(session), now)
                    self._recorded.pop(f"s:{key}", None)
                for key in [
                    k for k, kept in self._finished_status.items() if now - kept[2] > STATUS_KEEP
                ]:  # fmt: skip
                    del self._finished_status[key]
                for key in [
                    k
                    for k, j in self._jobs.items()
                    if j.finished and now - j.finished > FINISHED_KEEP
                ]:
                    del self._jobs[key]
                    self._recorded.pop(f"j:{key}", None)
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
                if not job.finished:
                    self._touch_job(job)
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
                            trusted.id, trusted.name, trusted.form, trusted.platform, "app",
                            online=False,
                        )  # fmt: skip
                        entry["paired"] = True
                        out.append(entry)
        if viewer.is_owner:
            for entry in out:
                pair = self.trust.get(entry["id"])
                if pair is not None:
                    entry["last_seen"] = pair.last_seen or None
        _tell_apart(out)
        return out

    def _incoming_view(self, s: IncomingSession) -> dict[str, Any]:
        return {
            "id": s.id,
            "from": {k: s.origin.get(k) for k in ("id", "name", "form", "platform", "via_name")},
            "paired": s.paired,
            "state": s.state,
            **describe(s.reason_code, s.reason),
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
                    "reason_code": f.reason_code,
                    "direct": f.direct,
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
            if d.session is not None:
                done = [i for i, f in enumerate(d.session.files) if f.state == DONE]
                failed = {
                    i: f.reason_code for i, f in enumerate(d.session.files) if f.state == FAILED
                }
            else:
                done, failed = sorted(d.files_done), dict(sorted(d.files_failed.items()))
            targets.append(
                {
                    **d.target,
                    "id": target_id,
                    "files_done": done,
                    "files_failed": failed,
                    "state": d.view_state(),
                    **describe(
                        d.session.reason_code if d.session else d.reason_code,
                        d.session.reason if d.session else d.reason,
                    ),
                    "sent": min(sent, total),
                    "direct": d.direct,
                }
            )
        for t in targets:
            t["disconnected"] = t["reason_code"] in DISCONNECT_CODES
        summary = summarize(
            [
                {"state": lifecycle_state(t["state"]), "reason_code": t["reason_code"]}
                for t in targets
            ]
        )
        return {
            "id": job.id,
            "files": job.files,
            "total": total,
            "targets": targets,
            "state": summary["state"],
            "summary": summary,
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
            state["pairing"] = self.pairing_view()
            with self._lock:
                state["pair_requests"] = [
                    self._pair_request_view(s)
                    for s in self._pair_sessions.values()
                    if s.state == "confirming" and time.monotonic() < s.confirm_by
                ]
            state["multicast"] = self.multicast_working()
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
        self.code = ""  # reason code when it failed
        self._finished = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ot-send", daemon=True)

    @property
    def alive(self) -> bool:
        return not self._finished.is_set() and not self.delivery.canceled

    def _count(self, n: int) -> None:
        self.delivery.sent += n
        if n:
            self.delivery.last_activity = time.monotonic()

    def start(self) -> None:
        self.delivery.busy += 1
        if self.delivery.session is None:
            self.mesh._move_delivery(self.delivery, "sending")
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
                    self.code = "transfer_stalled"
                    self.reader.aborted = True
                    return

    def finish(self) -> None:
        while self.alive:
            try:
                self.reader.queue.put(_EOF, timeout=0.5)
                return
            except queue.Full:
                continue
        if self.delivery.canceled:  # don't leave its thread waiting for more
            self.abort("Canceled", "sender_cancelled")

    def abort(self, reason: str, code: str = "sender_disconnected") -> None:
        self.error = self.error or reason
        self.code = self.code or code
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
            self.code = self.code or _send_error_code(exc)
            log.debug("delivery to %s failed", self.target_id, exc_info=True)
        finally:
            self.delivery.busy -= 1
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
            error = data.get("error") if isinstance(data, dict) else None
            code = str(error.get("code", "")) if isinstance(error, dict) else ""
            raise MeshError(
                res.status,
                code or "remote",
                _remote_message(data, "The receiver rejected the file."),
            )


#: ``error.code`` of a receiver's refusal -> reason code
_REMOTE_CODES = {
    "insufficient_storage": "insufficient_storage",
    "too_large": "file_too_large",
    "file_too_large": "file_too_large",
    "size_mismatch": "invalid_request",
    "duplicate": "invalid_request",
    "not_accepted": "invalid_request",
    "not_found": "receiver_disconnected",  # it restarted and forgot the transfer
}


def _send_error_code(exc: Exception) -> str:
    """Why sending a file to another app failed, as a reason code (before reconciling)."""
    if isinstance(exc, MeshError):
        return _REMOTE_CODES.get(
            exc.code,
            exc.code
            if exc.code in {"receiver_cancelled", "sender_cancelled"}
            else "unknown_failure",
        )
    if isinstance(exc, TimeoutError):
        return "network_timeout"
    if isinstance(exc, (ConnectionError, OSError)):
        return "receiver_disconnected"
    return "unknown_failure"


def _describe_error(exc: Exception, delivery: Delivery) -> str:
    if isinstance(exc, StorageError):
        return str(exc)
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return f"Lost connection to {delivery.target.get('name', 'the receiver')}."
    return "Transfer failed."


def _receive_error_code(exc: BaseException) -> str:
    """Why a file couldn't be received, as a reason code."""
    if isinstance(exc, InsufficientStorage):
        return "insufficient_storage"
    if isinstance(exc, TooLarge):
        return "file_too_large"
    if isinstance(exc, (StorageError, ConnectionError, TimeoutError)):
        return "sender_disconnected"  # the stream ended or broke before the last byte
    if isinstance(exc, OSError):
        return "destination_unavailable"  # couldn't write where files go
    if type(exc).__name__ == "ClientDisconnected":
        return "sender_disconnected"
    return "unknown_failure"


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


def _tell_apart(devices: list[dict[str, Any]]) -> None:
    """Devices with the same name must not look identical.

    An offline device that shares its name and type with one that is online
    (typically the same phone before a reinstall) is marked ``stale`` ("old
    device"); any others get a short id to tell them apart.
    """
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for d in devices:
        groups.setdefault((d["name"].casefold(), d["form"], d["kind"]), []).append(d)
    for same in groups.values():
        if len(same) < 2:
            continue
        online = [d for d in same if d["online"]]
        for d in same:
            d["short_id"] = d["id"][-4:]
            if online and not d["online"]:
                d["stale"] = True


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
