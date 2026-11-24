"""Small bounded retry policy for outbound provider calls."""

from __future__ import annotations

import time
from collections.abc import Callable
from urllib.error import HTTPError, URLError


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, HTTPError):
        return exc.code in {408, 429} or exc.code >= 500
    return isinstance(exc, TimeoutError | URLError)


def urlopen_with_retry(
    opener: Callable,
    target,
    *,
    timeout: float,
    retries: int = 3,
    backoff_seconds: float = 0.5,
    sleeper: Callable[[float], None] = time.sleep,
):
    """Open a URL/request with bounded exponential backoff for transient errors."""
    if retries < 0:
        raise ValueError("retries must not be negative")
    for attempt in range(retries + 1):
        try:
            return opener(target, timeout=timeout)
        except Exception as exc:
            if not _retryable(exc) or attempt == retries:
                raise
            sleeper(backoff_seconds * (2**attempt))
    raise RuntimeError("retry loop exhausted unexpectedly")
