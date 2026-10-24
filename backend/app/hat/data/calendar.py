"""NSE trading calendar: parse the holiday list, store it in `hat_trading_calendar`, and load it
as the one `SessionCalendar` every HAT reader takes (HAT-DATA-027, HAT-OVR-001, C7). It is loaded
by `session_calendar` and passed to its users; nothing installs it process-wide.

No HTTP here: the holiday text comes from a recorded file or a caller-supplied source.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from importlib import resources

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.time import IST

from ..models import HatMarket, HatTradingCalendar
from ..risk.sessions import SessionCalendar
from ..time import as_utc

# NSE's holiday master lists only the current year; the years before come from this recorded file
# (PR 72), so a history build from 2010 reads a past holiday as a holiday, not as a gap.
RECORDED_HISTORY = "nse_holidays_history.json"
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


def parse_recorded_history(text: str) -> list[CalendarDay]:
    """The recorded past years (`{"holidays": {year: [ISO dates]}, "sessions": [{"date",
    "note"}]}`): every listed holiday closed, every special session (Budget Saturday, Muhurat)
    a trading day without its own hours (none were recorded, so a reader takes the market's
    default session). A date that is both is a session. A session may fall in a year with no
    recorded holidays: one NSE's live holiday list leaves out (the 2026 Sunday Budget session).
    Raises on an unreadable file: it is checked in, so a bad one is a bug, not a bad answer."""
    payload = json.loads(text)
    days = {
        date.fromisoformat(raw): CalendarDay(date.fromisoformat(raw), False)
        for year in payload["holidays"].values()
        for raw in year
    }
    for entry in payload["sessions"]:
        day = date.fromisoformat(entry["date"])
        days[day] = CalendarDay(day, True, None, None, str(entry["note"])[:200] or None)
    return sorted(days.values(), key=lambda d: d.day)


def recorded_history() -> list[CalendarDay]:
    """The recorded file shipped beside this module."""
    return parse_recorded_history(
        resources.files(__package__).joinpath(RECORDED_HISTORY).read_text(encoding="utf-8")
    )


def with_recorded_sessions(days: Iterable[CalendarDay]) -> list[CalendarDay]:
    """`days` (the live holiday list) plus every recorded special session of the years it covers
    that it does not name itself: NSE's list leaves out a session announced by circular (the
    Sunday Budget session of 2026-02-01), and without its row the build never fetches the day."""
    listed = {d.day: d for d in days}
    years = {d.year for d in listed}
    for extra in recorded_history():
        if extra.is_trading and extra.day.year in years and extra.day not in listed:
            listed[extra.day] = extra
    return sorted(listed.values(), key=lambda d: d.day)


def store_recorded_history(
    session: Session, market: HatMarket, skip_years: Collection[int] = ()
) -> int:
    """Store the recorded past years with a row for every other weekday (`with_weekdays`), one
    full snapshot per year so a corrected file replaces what an earlier one stored. A year in
    `skip_years` (one the live holiday list covers) is left to that list. A recorded session of
    a year with no recorded holidays is stored on its own row, no snapshot (its weekdays are the
    live list's). Returns rows changed."""
    recorded = [d for d in recorded_history() if d.day.year not in skip_years]
    holiday_years = {d.day.year for d in recorded if not d.is_trading}
    by_year: dict[int, list[CalendarDay]] = {}
    for day in recorded:
        if day.day.year in holiday_years:
            by_year.setdefault(day.day.year, []).append(day)
    changed = sum(
        upsert_calendar(
            session,
            market.id,
            with_weekdays(days, market.session_open, market.session_close),
            replace_span=True,
        )
        for days in by_year.values()
    )
    sessions_only = [d for d in recorded if d.day.year not in holiday_years]
    return changed + upsert_calendar(session, market.id, sessions_only)


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


def session_calendar(session: Session, market_code: str = "NSE") -> SessionCalendar | None:
    """The market's `hat_trading_calendar` rows as the one session calendar every reader takes; None
    when the market is not set up. A trading row without its own times takes the `hat_market`
    default session (HAT-OVR-001), so every reader that counts the day as a session can open it."""
    market = session.scalar(select(HatMarket).where(HatMarket.code == market_code))
    if market is None:
        return None
    rows = session.scalars(
        select(HatTradingCalendar).where(HatTradingCalendar.market_id == market.id)
    )
    return SessionCalendar.from_rows(
        (
            r.day,
            r.is_trading,
            r.open_at or (_to_instant(r.day, market.session_open) if r.is_trading else None),
            r.close_at or (_to_instant(r.day, market.session_close) if r.is_trading else None),
        )
        for r in rows
    )
