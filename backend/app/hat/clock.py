"""Clock seam: HAT code that needs "now" takes a Clock, so tests can freeze time.

HAT owns this copy (K1, backend review C1); it imports nothing from the legacy `app.utils`.

`SystemClock` reads the wall clock; `FixedClock` holds a settable instant for tests.
Both return timezone-aware datetimes; convert with `.astimezone(...)` where a zone matters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """The current instant, timezone-aware."""
        ...


class SystemClock:
    """The real wall clock, in UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A clock that stays at one instant until moved with `set` or `advance`."""

    def __init__(self, instant: datetime) -> None:
        self._instant = require_aware(instant)

    def now(self) -> datetime:
        return self._instant

    def set(self, instant: datetime) -> None:
        self._instant = require_aware(instant)

    def advance(self, delta: timedelta) -> None:
        self._instant += delta


def require_aware(instant: datetime) -> datetime:
    """`instant` itself, or ValueError when it is naive (no usable UTC offset)."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Expected a timezone-aware datetime")
    return instant


SYSTEM_CLOCK = SystemClock()
