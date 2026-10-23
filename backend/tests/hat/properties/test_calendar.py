"""PBT calendar.* (HAT-DATA-027, HAT-OVR-001, HAT-SCH-086, HAT-RISK-075): the one trading calendar
(`SessionCalendar`, loaded by `session_calendar`) closes a holiday at every instant, opens a trading
day for its own session, gives a day it does not hold no session, and every reader (the feed, the
scheduler's trading days, Telegram's days held) answers as it does."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

from hat_mcp_support import reference_engine
from hat_risk_support import CALENDAR, TRADE_DAY, calendar_around
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session

from app.hat.clock import FixedClock
from app.hat.data import calendar as cal
from app.hat.data.feed import PREOPEN_FROM, LiveFeed
from app.hat.data.sessions import IST
from app.hat.models import HatMarket
from app.hat.risk.sessions import (
    SessionCalendar,
    SessionState,
    is_trading_day,
    next_session_open,
)
from app.hat.telegram.reads import _days_held

CalSpec = tuple[time, time, dict[date, cal.CalendarDay]]
WINDOW_START = date(2026, 1, 1)
DAYS = 21  # three weeks: weekdays, weekends and room for special sessions

minutes = st.integers(0, 24 * 60 - 1).map(lambda m: time(m // 60, m % 60))


@st.composite
def calendars(draw: st.DrawFn) -> CalSpec:
    """A market with its own default session and one calendar row for some of the days."""
    open_default = draw(st.integers(0, 12 * 60).map(lambda m: time(m // 60, m % 60)))
    close_default = (
        datetime.combine(WINDOW_START, open_default) + timedelta(minutes=draw(st.integers(30, 600)))
    ).time()
    if close_default <= open_default:
        close_default = time(23, 59)
    rows = {}
    for offset in range(DAYS):
        if not draw(st.booleans()):
            continue
        day = WINDOW_START + timedelta(days=offset)
        trading = draw(st.booleans())
        own = trading and draw(st.booleans())
        start = draw(minutes) if own else None
        end = (
            (datetime.combine(day, start) + timedelta(minutes=draw(st.integers(10, 300)))).time()
            if own
            else None
        )
        if own and end <= start:
            end = time(23, 59)
            if end <= start:
                start = time(0, 0)
        rows[day] = cal.CalendarDay(day, trading, start, end, None)
    return open_default, close_default, rows


def through_database(
    open_default: time, close_default: time, rows: dict[date, cal.CalendarDay]
) -> SessionCalendar:
    """Store the calendar in `hat_trading_calendar` and read it back, as the app does."""
    engine = reference_engine()
    with Session(engine) as session:
        market = HatMarket(
            code="NSE",
            timezone="Asia/Kolkata",
            currency="INR",
            session_open=open_default,
            session_close=close_default,
        )
        session.add(market)
        session.flush()
        cal.upsert_calendar(session, market.id, rows.values())
        session.commit()
        loaded = cal.session_calendar(session)
    assert loaded is not None
    return loaded


def instant(day: date, wall: time) -> datetime:
    return datetime.combine(day, wall, tzinfo=IST).astimezone(UTC)


GRID = [time(h, m) for h in range(24) for m in (0, 15, 30, 45)] + [time(9, 15), time(15, 30)]


def _session(row: cal.CalendarDay | None, default: tuple[time, time]) -> tuple[time, time] | None:
    """The session a stored row gives its day: its own times on a trading day (the market default
    for a time it lacks), else none."""
    if row is None or not row.is_trading:
        return None
    return row.open_at or default[0], row.close_at or default[1]


@settings(max_examples=60)
@given(spec=calendars())
def test_holiday_closed(spec: CalSpec) -> None:
    """For every date with is_trading = false the calendar is CLOSED at every instant of that date;
    a trading date is OPEN exactly from its open_at to its close_at (the `hat_market` default for a
    time the row lacks); a date it does not hold is UNKNOWN and never open."""
    open_default, close_default, rows = spec
    loaded = through_database(open_default, close_default, rows)
    for offset in range(DAYS):
        day = WINDOW_START + timedelta(days=offset)
        row = rows.get(day)
        session = _session(row, (open_default, close_default))
        for wall in GRID:
            state = loaded.state(instant(day, wall))
            if row is not None and not row.is_trading:
                assert state is SessionState.CLOSED
            elif session is None:
                assert state is SessionState.UNKNOWN, (day, wall)
            else:
                lo, hi = session
                want = SessionState.OPEN if lo <= wall <= hi else SessionState.CLOSED
                assert state is want, (day, wall)


@settings(max_examples=60)
@given(spec=calendars())
def test_holiday_closed_trading_days(spec: CalSpec) -> None:
    """For every calendar, the days the scheduler runs on (`is_trading_day`, `trading_days`) are
    exactly the dates stored with is_trading = true: a weekday without a row is not one."""
    open_default, close_default, rows = spec
    loaded = through_database(open_default, close_default, rows)
    days = set(loaded.trading_days())
    for offset in range(DAYS):
        day = WINDOW_START + timedelta(days=offset)
        row = rows.get(day)
        expected = row is not None and row.is_trading
        assert (day in days) == expected
        assert is_trading_day(loaded, day) == expected


@settings(max_examples=60)
@given(spec=calendars())
def test_session_from_reference_data(spec: CalSpec) -> None:
    """For every market and date the session the engine uses equals the `hat_trading_calendar`
    row, or the `hat_market` default that `with_weekdays` writes on a normal weekday, never a
    constant (HAT-OVR-001); an unlisted weekend has none."""
    open_default, close_default, rows = spec
    full = {d.day: d for d in cal.with_weekdays(rows.values(), open_default, close_default)}
    loaded = through_database(open_default, close_default, full)
    for offset in range(DAYS):
        day = WINDOW_START + timedelta(days=offset)
        expected = _session(full.get(day), (open_default, close_default))
        if rows and day not in rows:
            assert expected == ((open_default, close_default) if day.weekday() < 5 else None)
        opens = loaded.open_on(day)
        assert (None if opens is None else opens.astimezone(IST).time()) == (
            None if expected is None else expected[0]
        )
        for wall in GRID:
            is_open = loaded.is_open(instant(day, wall))
            assert is_open == (expected is not None and expected[0] <= wall <= expected[1])


@example(  # a weekday the calendar does not hold (HAT-OVR-001 left it open; the readers disagreed)
    spec=(time(9, 15), time(15, 30), {}), offset=1, wall=time(10, 0)
)
@settings(max_examples=80)
@given(
    spec=calendars(),
    offset=st.integers(0, DAYS - 1),
    wall=st.sampled_from(GRID + [PREOPEN_FROM, time(8, 59)]),
)
def test_one_answer(spec: CalSpec, offset: int, wall: time) -> None:
    """For every calendar and instant the readers agree with `SessionCalendar.state`: the feed may
    connect when the state is OPEN or in the pre-open from 09:00 before a trading day's open, and
    on no day the calendar does not hold; the scheduler's trading days and Telegram's days held
    count a day exactly when the calendar holds it as a trading day.
    Harm: a calendar that has run out drives a daily cycle and a live feed on a day risk rejects
    every entry as `market_closed`, or the readers count a different number of sessions."""
    open_default, close_default, rows = spec
    loaded = through_database(open_default, close_default, rows)
    day = WINDOW_START + timedelta(days=offset)
    at = instant(day, wall)
    state = loaded.state(at)
    opens = loaded.open_on(day)
    feed = LiveFeed(calendar=loaded, clock=FixedClock(at))
    preopen = opens is not None and at < opens and wall >= PREOPEN_FROM
    assert feed.connection_allowed() == (state is SessionState.OPEN or preopen)
    if state is SessionState.UNKNOWN:
        assert not feed.connection_allowed()
    trading = is_trading_day(loaded, day)
    row = rows.get(day)
    assert trading == (row is not None and row.is_trading)
    assert trading == (day in loaded.trading_days())
    if state is SessionState.OPEN:
        assert trading
    # and back: a day counted as a session has an open the feed and risk can reach
    assert trading == (opens is not None)
    opened = instant(WINDOW_START, time(10, 0))
    held = _days_held(loaded, opened, at)
    counted = [d for d in loaded.trading_days() if WINDOW_START <= d <= day]
    assert held == max(len(counted) - 1, 0)


@settings(max_examples=40)
@given(first=calendars(), second=calendars())
def test_holiday_closed_withdrawn(first: CalSpec, second: CalSpec) -> None:
    """A refreshed snapshot replaces the stored calendar inside its span: a holiday or session
    it no longer lists stops applying, and a repeat of the same snapshot changes nothing."""
    engine = reference_engine()
    with Session(engine) as session:
        market = HatMarket(
            code="NSE",
            timezone="Asia/Kolkata",
            currency="INR",
            session_open=time(9, 15),
            session_close=time(15, 30),
        )
        session.add(market)
        session.flush()
        cal.upsert_calendar(session, market.id, first[2].values(), replace_span=True)
        cal.upsert_calendar(session, market.id, second[2].values(), replace_span=True)
        loaded = cal.session_calendar(session)
        assert loaded is not None
        if second[2]:
            low, high = min(second[2]), max(second[2])
            stored = {d: r for d, r in loaded.days.items() if low <= d <= high}
            assert set(stored) == set(second[2])
            assert {d for d, r in stored.items() if not r.is_trading} == {
                d for d, r in second[2].items() if not r.is_trading
            }
            assert (
                cal.upsert_calendar(session, market.id, second[2].values(), replace_span=True) == 0
            )


# --- the next session open (HAT-SCH-086, HAT-DEC-B20) ---------------------------------------

session_instants = st.datetimes(
    min_value=datetime(2026, 10, 10), max_value=datetime(2026, 10, 31), timezones=st.just(IST)
)


@given(session_instants)
def test_next_session_open(at: datetime) -> None:
    """The next open is the earliest session open strictly after `at`; it is a trading day's
    09:15 IST, nothing earlier qualifies, and a calendar with no later session gives None."""
    found = next_session_open(CALENDAR, at)
    candidates = [
        datetime.combine(d, time(9, 15), tzinfo=IST)
        for d, row in CALENDAR.days.items()
        if row.is_trading
    ]
    later = [c for c in candidates if c > at]
    assert found == (min(later) if later else None)
    if found is not None:
        assert CALENDAR.state(found) is SessionState.OPEN
        assert found.astimezone(IST).time() == time(9, 15)
        assert found.astimezone(IST).weekday() < 5
    assert next_session_open(calendar_around(TRADE_DAY, span=0), at) in (
        None,
        datetime.combine(TRADE_DAY, time(9, 15), tzinfo=IST),
    )


@given(session_instants)
def test_next_session_open_after_a_holiday(at: datetime) -> None:
    """A holiday row is skipped: with Thursday a holiday, Wednesday evening goes to Friday."""
    days = dict(CALENDAR.days)
    thursday = TRADE_DAY + timedelta(days=1)
    days[thursday] = replace(days[thursday], is_trading=False)  # a holiday keeps its times
    holidays = type(CALENDAR)(days)
    evening = datetime.combine(TRADE_DAY, time(20, 0), tzinfo=IST)
    assert next_session_open(holidays, evening) == datetime.combine(
        thursday + timedelta(days=1), time(9, 15), tzinfo=IST
    )
    found = next_session_open(holidays, at)
    assert found is None or holidays.state(found) is SessionState.OPEN


@settings(max_examples=60)
@given(spec=calendars())
def test_weekday_rows(spec: CalSpec) -> None:
    """HAT-DATA-027 (PR 17): `with_weekdays` keeps every listed row as it is, gives every other
    weekday of the covered years a trading row with the market's default session, and adds no
    weekend: the holiday list names only the exceptions, but the scheduler and the risk checks
    read a session only from a stored row."""
    open_default, close_default, rows = spec
    full = cal.with_weekdays(rows.values(), open_default, close_default)
    by_day = {d.day: d for d in full}
    assert len(by_day) == len(full)
    assert [d.day for d in full] == sorted(by_day)
    for day, row in rows.items():
        assert by_day[day] == row
    if not rows:
        assert full == []
        return
    day = date(min(rows).year, 1, 1)
    while day <= date(max(rows).year, 12, 31):
        if day not in rows:
            expected = cal.CalendarDay(day, True, open_default, close_default)
            assert by_day.get(day) == (expected if day.weekday() < 5 else None)
        day += timedelta(days=1)
