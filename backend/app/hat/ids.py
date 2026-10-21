"""uuid v7 ids (HAT-SCH-011).

`uuid.uuid7` exists only on Python 3.14 and the repo targets 3.11+, so HAT generates its own.
Layout (RFC 9562 §5.7): 48-bit Unix milliseconds, version 7, a 12-bit counter, variant 10, 62
random bits. The counter makes ids from one generator strictly increasing in byte order even
within one millisecond or when the clock steps back.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from collections.abc import Callable

_MS_BITS = 48
_SEQ_MAX = 0xFFF
_MS_MAX = (1 << _MS_BITS) - 1


def _system_ms() -> int:
    return time.time_ns() // 1_000_000


class Uuid7Generator:
    """Thread-safe generator; `clock_ms` is injectable so a property can drive any clock."""

    def __init__(self, clock_ms: Callable[[], int] = _system_ms) -> None:
        self._clock_ms = clock_ms
        self._lock = threading.Lock()
        self._last_ms = -1
        self._seq = 0

    def next(self) -> uuid.UUID:
        with self._lock:
            ms = min(max(self._clock_ms(), self._last_ms), _MS_MAX)
            if ms > self._last_ms:
                # A new millisecond: start the counter low so a long burst does not overflow it.
                self._seq = secrets.randbelow(_SEQ_MAX // 2)
            elif self._seq < _SEQ_MAX:
                self._seq += 1
            else:
                # Counter exhausted inside one millisecond: borrow the next one.
                ms += 1
                self._seq = secrets.randbelow(_SEQ_MAX // 2)
            self._last_ms = ms
            seq = self._seq
        rand_b = secrets.randbits(62)
        value = (ms << 80) | (0x7 << 76) | (seq << 64) | (0b10 << 62) | rand_b
        return uuid.UUID(int=value)


_default = Uuid7Generator()


def uuid7() -> uuid.UUID:
    """A new uuid v7; each call in this process is greater than the one before."""
    return _default.next()
