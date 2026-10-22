"""PBT calendar.* (HAT-DATA-027, HAT-OVR-001, HAT-SCH-086): a calendar holiday is closed in
`MarketHoursPolicy` at every instant, a trading day uses its own session, and the engine's
session comes from market reference data, not constants."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

from hat_mcp_support import reference_engine
from hat_risk_support import CALENDAR, TRADE_DAY, calendar_around
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session

from app.hat.clock import FixedClock
from app.hat.data import calendar as cal
from app.hat.data.sessions import (
    IST,
    SESSION_CLOSE,
    SESSION_OPEN,
    MarketHoursPolicy,
    MarketPhase,
)
from app.hat.models import HatMarket
from app.hat.risk.sessions import SessionState, next_session_open

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
) -> cal.MarketCalendar:
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
        loaded = cal.load_calendar(session)
    assert loaded is not None
    return loaded


def instant(day: date, wall: time) -> datetime:
    return datetime.combine(day, wall, tzinfo=IST).astimezone(UTC)


GRID = [time(h, m) for h in range(24) for m in (0, 15, 30, 45)] + [time(9, 15), time(15, 30)]


@settings(max_examples=60)
@given(spec=calendars())
def test_holiday_closed(spec: CalSpec) -> None:
    """For every date with is_trading = false the policy reports the market closed at every
    instant of that date, and for every date with is_trading = true the session is
    open_at-close_at (the market default when the row gives none)."""
    open_default, close_default, rows = spec
    loaded = through_database(open_default, close_default, rows)
    policy = loaded.policy(FixedClock(instant(WINDOW_START, time(0, 0))))
    for offset in range(DAYS):
        day = WINDOW_START + timedelta(days=offset)
        row = rows.get(day)
        for wall in GRID:
            at = instant(day, wall)
            phase = policy.phase(at)
            if row is not None and not row.is_trading:
                assert not phase.is_open
                assert phase in (MarketPhase.HOLIDAY, MarketPhase.WEEKEND)
            elif row is not None:
                lo = row.open_at or open_default
                hi = row.close_at or close_default
                assert phase.is_open == (lo <= wall <= hi), (day, wall)


@settings(max_examples=30)
@given(spec=calendars())
def test_policy_has_no_process_state(spec: CalSpec) -> None:
    """A policy is its own calendar and nothing else (no installed global, no DEV_MODE): building
    one from a loaded calendar never changes a bare policy, so a bare policy is closed only on a
    weekend and open on every weekday inside the default session, whatever was loaded elsewhere."""
    open_default, close_default, rows = spec
    loaded = through_database(open_default, close_default, rows)
    loaded.policy(FixedClock(instant(WINDOW_START, time(0, 0))))
    bare = MarketHoursPolicy(clock=FixedClock(instant(WINDOW_START, time(0, 0))))
    for offset in range(DAYS):
        day = WINDOW_START + timedelta(days=offset)
        for wall in GRID:
            expected = day.weekday() < 5 and SESSION_OPEN <= wall <= SESSION_CLOSE
            assert bare.is_open(instant(day, wall)) == expected, (day, wall)


@settings(max_examples=60)
@given(spec=calendars())
def test_holiday_closed_trading_days(spec: CalSpec) -> None:
    """For every calendar, the set of days the scheduler runs on equals the set of dates with
    is_trading = true (where the calendar has a row), and weekdays without a row trade."""
    open_default, close_default, rows = spec
    loaded = through_database(open_default, close_default, rows)
    end = WINDOW_START + timedelta(days=DAYS - 1)
    days = cal.trading_days(loaded, WINDOW_START, end)
    for offset in range(DAYS):
        day = WINDOW_START + timedelta(days=offset)
        row = rows.get(day)
        expected = row.is_trading if row is not None else day.weekday() < 5
        assert (day in days) == expected


@settings(max_examples=60)
@given(spec=calendars())
def test_session_from_reference_data(spec: CalSpec) -> None:
    """For every market and date the session the engine uses equals the `hat_trading_calendar`
    row, or the `hat_market` default on a normal trading day, never a constant (HAT-OVR-001)."""
    open_default, close_default, rows = spec
    loaded = through_database(open_default, close_default, rows)
    policy = loaded.policy(FixedClock(instant(WINDOW_START, time(0, 0))))
    for offset in range(DAYS):
        day = WINDOW_START + timedelta(days=offset)
        row = rows.get(day)
        if row is not None and row.is_trading:
            expected = (row.open_at or open_default, row.close_at or close_default)
        elif row is None and day.weekday() < 5:
            expected = (open_default, close_default)
        else:
            expected = None
        assert loaded.session_for(day) == expected
        for wall in GRID:
            is_open = policy.is_open(instant(day, wall))
            assert is_open == (expected is not None and expected[0] <= wall <= expected[1])


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
        loaded = cal.load_calendar(session)
        assert loaded is not None
        if second[2]:
            low, high = min(second[2]), max(second[2])
            stored = {d: r for d, r in loaded.rows.items() if low <= d <= high}
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
