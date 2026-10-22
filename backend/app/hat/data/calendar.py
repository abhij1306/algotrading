"""NSE trading calendar: parse the holiday list, store it in `hat_trading_calendar`, and feed
the `MarketHoursPolicy` the HAT engine reads (HAT-DATA-027, HAT-OVR-001, C7). The policy is built
from a loaded calendar and passed to its users; nothing installs it process-wide.

No HTTP here: the holiday text comes from a recorded file or a caller-supplied source.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.clock import SYSTEM_CLOCK, Clock
from app.hat.data.sessions import IST, MarketHoursPolicy

from ..models import HatMarket, HatTradingCalendar
from ..time import as_utc

_DATE_FORMATS = ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d")
_SESSION_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


@dataclass(frozen=True)
class CalendarDay:
    day: date
    is_trading: bool
    open_at: time | None = None  # IST wall-clock, for a special session
    close_at: time | None = None
    note: str | None = None


def _parse_date(raw: str) -> date | None:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _parse_session(raw: object) -> tuple[time, time] | None:
    if not isinstance(raw, str):
        return None
    match = _SESSION_RE.match(raw.strip())
    if match is None:
        return None
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    try:
        start, end = time(h1, m1), time(h2, m2)
    except ValueError:
        return None
    return (start, end) if start < end else None


def parse_nse_holidays(text: str) -> tuple[list[CalendarDay], int]:
    """Parse NSE's holiday-master JSON (`{"CM": [{"tradingDate", "description",
    "morning_session", "evening_session"}]}`) into calendar days, plus the number of entries
    skipped as unreadable. An entry with a session window (such as Muhurat trading) is a
    trading day with its own hours; any other entry is a holiday. Day-level format is `[A]`:
    validated against the first live download.
    """
    payload = json.loads(text)
    entries = payload.get("CM") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return [], 0
    days: dict[date, CalendarDay] = {}
    skipped = 0
    for entry in entries:
        day = _parse_date(str(entry.get("tradingDate", ""))) if isinstance(entry, dict) else None
        if day is None:
            skipped += 1
            continue
        note = str(entry.get("description") or "")[:200] or None
        session = _parse_session(entry.get("evening_session")) or _parse_session(
            entry.get("morning_session")
        )
        if session is not None:
            days[day] = CalendarDay(day, True, session[0], session[1], note)
        else:
            days[day] = CalendarDay(day, False, None, None, note)
    return sorted(days.values(), key=lambda d: d.day), skipped


def with_weekdays(
    days: Iterable[CalendarDay], session_open: time, session_close: time
) -> list[CalendarDay]:
    """`days` plus every other weekday of the calendar years they cover, as a trading day with
    the market's default session (PR 17). The holiday list names only exceptions, but the risk
    checks and the scheduler read a session only from a stored row (HAT-RISK-075, HAT-OPS-012),
    so an ordinary weekday needs its row too. A weekend not listed stays without a row: closed."""
    listed = {d.day: d for d in days}
    if not listed:
        return []
    day = date(min(listed).year, 1, 1)
    last = date(max(listed).year, 12, 31)
    out = dict(listed)
    while day <= last:
        if day not in out and day.weekday() < 5:
            out[day] = CalendarDay(day, True, session_open, session_close)
        day += timedelta(days=1)
    return sorted(out.values(), key=lambda d: d.day)


def _to_instant(day: date, wall: time) -> datetime:
    return as_utc(datetime.combine(day, wall, tzinfo=IST))


def _same_row(
    row: HatTradingCalendar, item: CalendarDay, window: tuple[datetime | None, datetime | None]
) -> bool:
    return (
        row.is_trading,
        None if row.open_at is None else as_utc(row.open_at),
        None if row.close_at is None else as_utc(row.close_at),
        row.note,
    ) == (item.is_trading, window[0], window[1], item.note)


def upsert_calendar(
    session: Session,
    market_id: uuid.UUID,
    days: Iterable[CalendarDay],
    *,
    replace_span: bool = False,
) -> int:
    """Insert or update calendar rows; returns how many changed. Idempotent. With
    `replace_span`, `days` is a full snapshot: stored rows inside its date span that it no
    longer lists are removed, so a withdrawn holiday or session stops applying."""
    snapshot = list(days)
    changed = 0
    for item in snapshot:
        window = (
            _to_instant(item.day, item.open_at) if item.open_at else None,
            _to_instant(item.day, item.close_at) if item.close_at else None,
        )
        row = session.get(HatTradingCalendar, (market_id, item.day))
        if row is None:
            session.add(
                HatTradingCalendar(
                    market_id=market_id,
                    day=item.day,
                    is_trading=item.is_trading,
                    open_at=window[0],
                    close_at=window[1],
                    note=item.note,
                )
            )
            changed += 1
        elif not _same_row(row, item, window):
            row.is_trading, row.open_at, row.close_at, row.note = (
                item.is_trading,
                window[0],
                window[1],
                item.note,
            )
            changed += 1
    if replace_span and snapshot:
        listed = {item.day for item in snapshot}
        first, last = min(listed), max(listed)
        for row in session.scalars(
            select(HatTradingCalendar).where(
                HatTradingCalendar.market_id == market_id,
                HatTradingCalendar.day >= first,
                HatTradingCalendar.day <= last,
            )
        ):
            if row.day not in listed:
                session.delete(row)
                changed += 1
    session.flush()
    return changed


@dataclass(frozen=True)
class MarketCalendar:
    """What the engine reads: the market's default session plus calendar rows by date."""

    session_open: time
    session_close: time
    rows: Mapping[date, CalendarDay]

    def holidays(self) -> frozenset[date]:
        return frozenset(d for d, r in self.rows.items() if not r.is_trading)

    def sessions(self) -> dict[date, tuple[time, time]]:
        """Own (open, close) of every date the calendar marks as a trading day."""
        return {
            d: (r.open_at or self.session_open, r.close_at or self.session_close)
            for d, r in self.rows.items()
            if r.is_trading
        }

    def policy(self, clock: Clock = SYSTEM_CLOCK) -> MarketHoursPolicy:
        """A `MarketHoursPolicy` that reads this calendar and the market's default session."""
        return MarketHoursPolicy(
            clock=clock,
            holidays=self.holidays(),
            sessions=self.sessions(),
            session_open=self.session_open,
            session_close=self.session_close,
        )

    def session_for(self, day: date) -> tuple[time, time] | None:
        """The session the engine uses on `day`, or None when the market is closed: the calendar
        row if there is one, else the market default on a weekday (HAT-OVR-001)."""
        row = self.rows.get(day)
        if row is not None:
            if not row.is_trading:
                return None
            return (row.open_at or self.session_open, row.close_at or self.session_close)
        return (self.session_open, self.session_close) if day.weekday() < 5 else None


def trading_days(calendar: MarketCalendar, start: date, end: date) -> set[date]:
    """Dates in [start, end] with a session: what the scheduler runs its daily cycle on."""
    out = set()
    day = start
    while day <= end:
        if calendar.session_for(day) is not None:
            out.add(day)
        day += timedelta(days=1)
    return out


def load_calendar(session: Session, market_code: str = "NSE") -> MarketCalendar | None:
    market = session.scalar(select(HatMarket).where(HatMarket.code == market_code))
    if market is None:
        return None
    rows = {}
    for row in session.scalars(
        select(HatTradingCalendar).where(HatTradingCalendar.market_id == market.id)
    ):
        open_at = as_utc(row.open_at).astimezone(IST).time() if row.open_at else None
        close_at = as_utc(row.close_at).astimezone(IST).time() if row.close_at else None
        rows[row.day] = CalendarDay(row.day, row.is_trading, open_at, close_at, row.note)
    return MarketCalendar(market.session_open, market.session_close, rows)
