"""Helpers for figuring out how other devices can reach this computer."""

from __future__ import annotations

import ipaddress
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import closing
from typing import Any


def _is_usable(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.version == 4 and not (addr.is_loopback or addr.is_link_local or addr.is_unspecified)


def primary_ip() -> str | None:
    """The IPv4 address of the interface used for outbound traffic.

    Connecting a UDP socket sends no packets; it only asks the OS which
    interface it *would* route through. This works offline on a LAN too,
    unlike the old ``gethostbyname(gethostname())`` which often returned
    ``127.0.1.1`` on Linux.
    """
    for probe in ("10.255.255.255", "192.168.255.255", "8.8.8.8"):
        try:
            with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as sock:
                sock.connect((probe, 1))
                ip: str = sock.getsockname()[0]
        except OSError:
            continue
        if _is_usable(ip):
            return ip
    return None


_HOST_IPS_TTL = 30.0
_host_ips_cache: list[str] | None = None
_host_ips_at = 0.0
_host_ips_lock = threading.Lock()
_host_ips_refreshing = threading.Event()


def _resolve_host_ips() -> list[str]:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    return [str(info[4][0]) for info in infos if _is_usable(str(info[4][0]))]


def _refresh_host_ips() -> None:
    global _host_ips_cache, _host_ips_at
    try:
        ips = _resolve_host_ips()
        with _host_ips_lock:
            _host_ips_cache, _host_ips_at = ips, time.monotonic()
    finally:
        _host_ips_refreshing.clear()


def _host_ips(wait: float = 1.0) -> list[str]:
    """This machine's addresses from its host name, cached.

    Resolving the host name can block for seconds (macOS tries mDNS for
    ``name.local``), so it runs on a background thread and callers wait at
    most ``wait`` seconds the first time; later calls never block.
    """
    with _host_ips_lock:
        cached, fresh = _host_ips_cache, time.monotonic() - _host_ips_at < _HOST_IPS_TTL
    if cached is not None and fresh:
        return cached
    if not _host_ips_refreshing.is_set():
        _host_ips_refreshing.set()
        thread = threading.Thread(target=_refresh_host_ips, name="ot-host-ips", daemon=True)
        thread.start()
        if cached is None:
            thread.join(wait)
    with _host_ips_lock:
        return list(_host_ips_cache or [])


def lan_ips() -> list[str]:
    """All usable IPv4 addresses of this machine, primary first."""
    found: list[str] = []
    primary = primary_ip()
    if primary:
        found.append(primary)
    for ip in _host_ips():
        if ip not in found:
            found.append(ip)
    return found


def hostname() -> str:
    name = socket.gethostname() or "this computer"
    return name.removesuffix(".local").removesuffix(".lan")


def port_is_free(host: str, port: int) -> bool:
    # macOS lets one address bind next to a listener on every address (0.0.0.0),
    # and then the two share the port, so check that too.
    for addr in dict.fromkeys((host, "0.0.0.0")):
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            # Match the server socket (cheroot sets SO_REUSEADDR) so a port that only
            # has TIME_WAIT leftovers from a restart counts as free. Not on Windows,
            # where SO_REUSEADDR would let us "share" a port that is really in use.
            if sys.platform != "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((addr, port))
            except OSError:
                return False
    return True


def find_free_port(host: str, preferred: int, attempts: int = 20) -> int:
    """``preferred`` if it is free, otherwise the next free port above it.

    macOS uses port 5000 for AirPlay Receiver, so falling back matters.
    """
    if preferred == 0:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            sock.bind((host, 0))
            return int(sock.getsockname()[1])
    for port in range(preferred, min(preferred + attempts, 65536)):
        if port_is_free(host, port):
            return port
    raise OSError(f"No free port found between {preferred} and {preferred + attempts - 1}")


PUBLIC_NETWORKS = (
    "Get-NetConnectionProfile | Where-Object NetworkCategory -eq 'Public' | ForEach-Object Name"
)


def public_networks(
    run: Callable[..., Any] = subprocess.run, platform: str = sys.platform
) -> list[str]:
    """Windows networks set to "Public": Windows Firewall keeps other devices from reaching
    apps on them, even ones allowed on Private networks. Empty elsewhere or on failure."""
    if platform != "win32":
        return []
    try:
        done = run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", PUBLIC_NETWORKS],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    names = [line.strip() for line in str(done.stdout or "").splitlines() if line.strip()]
    return names[:4]
