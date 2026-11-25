"""Small, process-local sliding-window limiter for the hosted API boundary."""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


class SlidingWindowRateLimiter:
    """Bound request admission per resolved caller identity.

    The limiter is intentionally process-local: it protects a single worker and
    adds no paid dependency. Multi-instance deployments should put a shared
    gateway limiter in front of the service for global enforcement.
    """

    def __init__(self, limit: int = 120, window_seconds: int = 60):
        self.limit = max(0, int(limit))
        self.window_seconds = max(1, int(window_seconds))
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> SlidingWindowRateLimiter:
        return cls(
            limit=int(os.environ.get("BIOLAB_RATE_LIMIT_PER_MINUTE", "120")),
            window_seconds=int(os.environ.get("BIOLAB_RATE_LIMIT_WINDOW_SECONDS", "60")),
        )

    def check(self, identity: str, now: float | None = None) -> Decision:
        if self.limit == 0:
            return Decision(True, 0, 0, 0)
        current = time.monotonic() if now is None else now
        cutoff = current - self.window_seconds
        with self._lock:
            events = self._events[identity]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                retry_after = max(1, int(events[0] + self.window_seconds - current + 0.999))
                return Decision(False, self.limit, 0, retry_after)
            events.append(current)
            return Decision(True, self.limit, self.limit - len(events), 0)
