"""Time helpers: UTC-aware instants and exchange-local (IST) trade dates (HAT-SCH-015)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.hat.clock import SYSTEM_CLOCK, Clock, require_aware

IST = ZoneInfo("Asia/Kolkata")  # NSE's exchange zone

__all__ = ["IST", "as_utc", "day_bounds", "trade_date_of", "utc_now"]


def utc_now(clock: Clock = SYSTEM_CLOCK) -> datetime:
    """The current instant as an aware UTC datetime (the default for creation timestamps)."""
    return clock.now().astimezone(UTC)


def as_utc(instant: datetime) -> datetime:
    """`instant` in UTC; a naive value (SQLite returns these) is taken to already be UTC."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        return instant.replace(tzinfo=UTC)
    return instant.astimezone(UTC)


def trade_date_of(instant: datetime) -> date:
    """The exchange-local calendar date of an aware instant (NSE: Asia/Kolkata)."""
    return require_aware(instant).astimezone(IST).date()


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """The UTC instants where the exchange-local (IST) day `day` starts and the next one starts:
    the window of everything whose trade date is `day`."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=IST)
    return as_utc(start), as_utc(start + timedelta(days=1))
