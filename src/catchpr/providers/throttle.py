"""Minimum interval between outbound model calls."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class Throttle:
    def __init__(self, min_interval: float, sleep: Callable[[float], None] | None = None) -> None:
        if min_interval < 0:
            raise ValueError("min_interval must be >= 0")
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0
        self._sleep = time.sleep if sleep is None else sleep

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            if delay > 0:
                self._sleep(delay)
                now = time.monotonic()
            self._next = now + self.min_interval
