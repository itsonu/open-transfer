"""Find other Open Transfer devices on the local network.

Every app announces itself with a small JSON datagram to an admin-scoped
multicast group every few seconds, and answers new devices straight away.
Datagrams carry only who a device is and which TCP port its HTTP server
listens on; everything else (presence of browser visitors, transfers,
pairing) happens over HTTP between the devices themselves.

Multicast is often filtered on guest Wi-Fi and some routers. The mesh
therefore also keeps peers alive with direct HTTP "hello"s, and devices can
be added by address or found by their pairing code.
"""

from __future__ import annotations

import contextlib
import json
import logging
import random
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from open_transfer import network

log = logging.getLogger("open_transfer")

GROUP = "239.255.77.77"
PORT = 47823
APP = "open-transfer"
VERSION = 1
MAX_PACKET = 1400
ANNOUNCE_EVERY = 4.0
REJOIN_EVERY = 30.0

PacketHandler = Callable[[dict[str, Any], str], None]


def encode(packet: dict[str, Any]) -> bytes:
    data = json.dumps({"app": APP, "v": VERSION, **packet}, separators=(",", ":")).encode()
    if len(data) > MAX_PACKET:
        raise ValueError("discovery packet too large")
    return data


def decode(data: bytes) -> dict[str, Any] | None:
    """Parse a datagram, or ``None`` if it isn't a valid Open Transfer packet."""
    if len(data) > MAX_PACKET:
        return None
    try:
        packet = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(packet, dict) or packet.get("app") != APP:
        return None
    if not isinstance(packet.get("v"), int) or packet["v"] < 1:
        return None
    if packet.get("type") not in {"announce", "reply", "find", "bye"}:
        return None
    if not isinstance(packet.get("id"), str):
        return None
    if packet["type"] in {"announce", "reply"}:
        port = packet.get("port")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            return None
    return packet


class Discovery:
    """Sends and receives discovery datagrams on a background thread."""

    def __init__(
        self,
        describe: Callable[[], dict[str, Any]],
        on_packet: PacketHandler,
        *,
        port: int = PORT,
        group: str = GROUP,
        interval: float = ANNOUNCE_EVERY,
    ) -> None:
        self._describe = describe
        self._on_packet = on_packet
        self.port = port
        self.group = group
        self.interval = interval
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._joined: set[str] = set()
        self._last_reply = 0.0
        self.working = False  # True once we have bound the socket

    # ---------------------------------------------------------------- control

    def start(self) -> None:
        try:
            self._sock = self._open_socket()
        except OSError as exc:
            log.warning("Nearby-device discovery is off: %s", exc)
            return
        self.working = True
        self._join_groups()
        self._thread = threading.Thread(target=self._run, name="ot-discovery", daemon=True)
        self._thread.start()
        log.debug("Discovery on %s:%s", self.group, self.port)

    def stop(self) -> None:
        if not self._sock:
            return
        with contextlib.suppress(OSError):
            self.send({"type": "bye"})
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        with contextlib.suppress(OSError):
            self._sock.close()
        self._sock = None

    # ---------------------------------------------------------------- sending

    def announce(self, *, reply: bool = False) -> None:
        packet = {"type": "reply" if reply else "announce", **self._describe()}
        self.send(packet)

    def find(self) -> None:
        """Ask every device whose pairing window is open to answer.

        Nothing about the code is sent: which device shows it is settled by the
        pairing exchange itself (docs/trust-model.md).
        """
        self.send({"type": "find", "id": self._describe()["id"]})

    def send_to(self, packet: dict[str, Any], address: str) -> None:
        """Answer one device directly (works where multicast only goes one way)."""
        sock = self._sock
        if sock is None:
            return
        if "id" not in packet:
            packet = {**packet, "id": self._describe()["id"]}
        with contextlib.suppress(OSError, ValueError):
            sock.sendto(encode(packet), (address, self.port))

    def send(self, packet: dict[str, Any]) -> None:
        sock = self._sock
        if sock is None:
            return
        if "id" not in packet:
            packet = {**packet, "id": self._describe()["id"]}
        data = encode(packet)
        sent = False
        with self._send_lock:
            for ip in self._interfaces():
                try:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                    sock.sendto(data, (self.group, self.port))
                    sent = True
                except OSError:
                    continue
        if not sent:
            log.debug("could not send a discovery packet on any interface")

    # -------------------------------------------------------------- internals

    def _interfaces(self) -> list[str]:
        ips = network.lan_ips()
        return ips or ["0.0.0.0"]

    def _open_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT") and sys.platform != "win32":
            # Lets several Open Transfer instances share the port on one machine.
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", self.port))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        sock.settimeout(1.0)
        return sock

    def _join_groups(self) -> None:
        """Join the group on every LAN interface (re-run when Wi-Fi changes)."""
        sock = self._sock
        if sock is None:
            return
        wanted = set(self._interfaces())
        for ip in sorted(wanted - self._joined):
            membership = struct.pack("4s4s", socket.inet_aton(self.group), socket.inet_aton(ip))
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
                self._joined.add(ip)
            except OSError as exc:
                log.debug("could not join %s on %s: %s", self.group, ip, exc)

    def _run(self) -> None:
        sock = self._sock
        assert sock is not None  # noqa: S101 - set in start()
        # A few quick announcements so others notice us within a second.
        next_announce = time.monotonic()
        burst = [0.4, 1.2]
        next_rejoin = time.monotonic() + REJOIN_EVERY
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_announce:
                with contextlib.suppress(OSError, ValueError):
                    self.announce()
                delay = burst.pop(0) if burst else self.interval * random.uniform(0.85, 1.15)  # noqa: S311
                next_announce = now + delay
            if now >= next_rejoin:
                self._join_groups()
                next_rejoin = now + REJOIN_EVERY
            try:
                data, (src, _) = sock.recvfrom(MAX_PACKET + 1)
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(0.5)
                continue
            packet = decode(data)
            if packet is None:
                continue
            try:
                self._on_packet(packet, src)
            except Exception:  # never let one bad packet stop discovery
                log.debug("discovery handler failed", exc_info=True)

    def reply_soon(self) -> None:
        """Answer a newcomer, at most once a second."""
        now = time.monotonic()
        if now - self._last_reply < 1.0:
            return
        self._last_reply = now
        with contextlib.suppress(OSError, ValueError):
            self.announce(reply=True)
