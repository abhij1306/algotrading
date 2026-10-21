"""Cut-down counters and throttled logging for the provider socket (K3; replaces the legacy
`runtime_metrics` and `log_exception_throttled`, which HAT does not import).

Thread-safe: the SDK threads and the event loop both count here.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

__all__ = ["TRACEBACK_INTERVAL_S", "FeedMetrics", "feed_metrics", "log_exception_throttled"]

# At most one full traceback per site in this many seconds.
TRACEBACK_INTERVAL_S = 60.0


class FeedMetrics:
    """Named counters and gauges; no timings, no labels."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}

    def increment(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + value

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def snapshot(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {"counters": dict(self._counters), "gauges": dict(self._gauges)}


feed_metrics = FeedMetrics()

_lock = threading.Lock()
_last_traceback_at: dict[str, float] = {}


def log_exception_throttled(
    logger: logging.Logger,
    site: str,
    msg: str,
    *args: object,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Call inside an except block: `logger.exception` the first time and then at most once per
    `TRACEBACK_INTERVAL_S` for `site`; in between, one debug line without the traceback, counted
    as `logging.suppressed_tracebacks.<site>`. Returns True when the traceback was written."""
    now = clock()
    with _lock:
        last = _last_traceback_at.get(site)
        full = last is None or now - last >= TRACEBACK_INTERVAL_S
        if full:
            _last_traceback_at[site] = now
    if full:
        logger.exception(msg, *args)
        return True
    feed_metrics.increment(f"logging.suppressed_tracebacks.{site}")
    logger.debug(msg, *args)
    return False
