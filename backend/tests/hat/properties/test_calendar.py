"""PBT calendar.* (HAT-DATA-027, HAT-OVR-001): a calendar holiday is closed in
`MarketHoursPolicy` at every instant, a trading day uses its own session, and the engine's
session comes from market reference data, not constants."""

from datetime import UTC, date, datetime, time, timedelta

from hat_mcp_support import reference_engine
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session

from app.hat.data import calendar as cal
from app.hat.models import HatMarket
from app.utils import market_hours
from app.utils.clock import FixedClock
from app.utils.market_hours import IST, MarketHoursPolicy, MarketPhase

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
            phase = policy.phase(at, dev_mode=False)
            if row is not None and not row.is_trading:
                assert not phase.is_open
                assert phase in (MarketPhase.HOLIDAY, MarketPhase.WEEKEND)
            elif row is not None:
                lo = row.open_at or open_default
                hi = row.close_at or close_default
                assert phase.is_open == (lo <= wall <= hi), (day, wall)


@settings(max_examples=30)
@given(spec=calendars())
def test_holiday_closed_installed_calendar(spec: CalSpec) -> None:
    """The installed calendar (the startup path) closes every policy, including one built
    without any holidays of its own, the way live_market_service builds its policy."""
    open_default, close_default, rows = spec
    loaded = through_database(open_default, close_default, rows)
    market_hours.install_calendar(
        loaded.holidays(), loaded.sessions(), loaded.session_open, loaded.session_close
    )
    try:
        bare = MarketHoursPolicy(clock=FixedClock(instant(WINDOW_START, time(0, 0))))
        for offset in range(DAYS):  # ordinary weekdays use the market's default hours
            day = WINDOW_START + timedelta(days=offset)
            if day not in rows and day.weekday() < 5:
                for wall in GRID:
                    expected = open_default <= wall <= close_default
                    assert bare.is_open(instant(day, wall), dev_mode=False) == expected
        for day, row in rows.items():
            if not row.is_trading:
                for wall in GRID:
                    assert not bare.is_open(instant(day, wall), dev_mode=False)
                    assert not market_hours.DEFAULT_POLICY.is_open(
                        instant(day, wall), dev_mode=False
                    )
    finally:
        market_hours.install_calendar(frozenset(), {})


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
            is_open = policy.is_open(instant(day, wall), dev_mode=False)
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
