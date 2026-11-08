"""A stopwatch that only advances while recording (not while paused).

Every timestamp in a session (audio position, speaking events) uses this clock,
so audio and speaker events line up even across breaks.
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class SessionClock:
    def __init__(self, now: Callable[[], float] = time.monotonic):
        self._now = now
        self._lock = threading.Lock()
        self._accum = 0.0
        self._running_since: float | None = None
        self.started = False

    def start(self) -> None:
        with self._lock:
            self.started = True
            self._accum = 0.0
            self._running_since = self._now()

    def pause(self) -> float:
        with self._lock:
            if self._running_since is not None:
                self._accum += self._now() - self._running_since
                self._running_since = None
            return self._accum

    def resume(self) -> float:
        with self._lock:
            if self._running_since is None and self.started:
                self._running_since = self._now()
            return self._accum

    @property
    def running(self) -> bool:
        return self._running_since is not None

    def elapsed(self) -> float:
        with self._lock:
            if self._running_since is None:
                return self._accum
            return self._accum + (self._now() - self._running_since)
