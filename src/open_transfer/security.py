"""Request hardening: host/origin checks, PIN auth, rate limiting and headers."""

from __future__ import annotations

import hmac
import ipaddress
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from flask import Request, Response

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localhost")

CSP = "; ".join(
    [
        "default-src 'self'",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        "style-src 'self'",
        "script-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)


def _split_host(value: str) -> str:
    """``"[::1]:5000"`` → ``"::1"``, ``"example.com:80"`` → ``"example.com"``."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


def host_allowed(
    host_header: str, allowed: tuple[str, ...], machine_names: tuple[str, ...]
) -> bool:
    """Defend against DNS rebinding.

    A malicious web page can point its own domain at your LAN IP and then read
    your shared files "same-origin". We only accept Host headers that are IP
    literals, ``localhost``, typical LAN suffixes, this machine's own name, or
    names the operator explicitly allowed.
    """
    if "*" in allowed:
        return True
    host = _split_host(host_header)
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return True
    return host in allowed or host in machine_names


def same_origin(request: Request) -> bool:
    """Reject cross-site state changes (CSRF) from browsers.

    Browsers always send ``Sec-Fetch-Site`` and/or ``Origin`` on cross-site
    POST/PUT/DELETE. Non-browser clients (curl, scripts) send neither and are
    allowed through — they are not exposed to CSRF.
    """
    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return False
    origin = request.headers.get("Origin")
    if origin and origin != "null":
        return urlsplit(origin).netloc.lower() == request.host.lower()
    return origin != "null"


def pin_matches(expected: str, supplied: str) -> bool:
    return hmac.compare_digest(expected.encode(), (supplied or "").strip().encode())


class RateLimiter:
    """Sliding-window limiter for failed PIN attempts, per client address."""

    def __init__(self, attempts: int = 5, window: float = 60.0) -> None:
        self.attempts = attempts
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        return hits

    def retry_after(self, key: str) -> float:
        """Seconds until ``key`` may try again (0 if allowed now)."""
        now = time.monotonic()
        with self._lock:
            hits = self._prune(key, now)
            if len(hits) < self.attempts:
                return 0.0
            return max(0.0, self.window - (now - hits[0]))

    def hit(self, key: str) -> None:
        with self._lock:
            self._prune(key, time.monotonic()).append(time.monotonic())

    def attempt(self, key: str) -> float:
        """Atomically reserve one attempt for ``key``.

        Returns 0 when the attempt is allowed (and counts it), otherwise the
        seconds to wait. Checking and counting under one lock means a burst
        of parallel requests can't all slip past the limit. Call
        :meth:`reset` after a successful attempt.
        """
        now = time.monotonic()
        with self._lock:
            hits = self._prune(key, now)
            if len(hits) >= self.attempts:
                return max(0.001, self.window - (now - hits[0]))
            hits.append(now)
            return 0.0

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


def log_safe(value: object) -> str:
    """Neutralise line breaks so client-supplied text can't forge log lines."""
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def apply_security_headers(response: Response) -> Response:
    headers = response.headers
    headers.setdefault("Content-Security-Policy", CSP)
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "no-referrer")
    headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
    return response
