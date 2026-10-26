"""Hybrid logical clock: orders copies across devices whose clocks disagree a little.

A timestamp is ``(wall_ms, counter)``. Every local copy gets a timestamp
above everything this device has seen, so "newest wins" can't be fooled by a
clock that runs a few seconds behind.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from open_transfer.sync.protocol import SyncError

#: Timestamps further ahead of our own clock than this are refused, so one
#: device with a wrong clock can't make every later copy look stale.
MAX_AHEAD_MS = 5 * 60 * 1000

Stamp = tuple[int, int]


class HybridClock:
    def __init__(self, wall: Callable[[], float] = time.time) -> None:
        self._wall = wall
        self._lock = threading.Lock()
        self._ms = 0
        self._counter = 0

    def _now(self) -> int:
        return int(self._wall() * 1000)

    def tick(self) -> Stamp:
        with self._lock:
            now = self._now()
            if now > self._ms:
                self._ms, self._counter = now, 0
            else:
                self._counter += 1
            return self._ms, self._counter

    def observe(self, stamp: Stamp) -> None:
        ms, counter = stamp
        with self._lock:
            now = self._now()
            if ms > now + MAX_AHEAD_MS:
                raise SyncError(400, "clock", "That device's clock is too far ahead.")
            top = max(now, self._ms, ms)
            if top == self._ms == ms:
                self._counter = max(self._counter, counter) + 1
            elif top == self._ms:
                self._counter += 1
            elif top == ms:
                self._counter = counter + 1
            else:
                self._counter = 0
            self._ms = top
