"""PBT calendar.*: the one trading calendar
(`SessionCalendar`, loaded by `session_calendar`) closes a holiday at every instant, opens a trading
day for its own session, gives a day it does not hold no session, and every reader (the feed, the
scheduler's trading days, Telegram's days held) answers as it does."""

from datetime import UTC, date, datetime, time, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from app.hat.data import calendar as cal
from app.hat.time import IST

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


def instant(day: date, wall: time) -> datetime:
    return datetime.combine(day, wall, tzinfo=IST).astimezone(UTC)


def _session(row: cal.CalendarDay | None, default: tuple[time, time]) -> tuple[time, time] | None:
    """The session a stored row gives its day: its own times on a trading day (the market default
    for a time it lacks), else none."""
    if row is None or not row.is_trading:
        return None
    return row.open_at or default[0], row.close_at or default[1]


# --- the next session open ---------------------------------------


@settings(max_examples=60)
@given(spec=calendars())
def test_weekday_rows(spec: CalSpec) -> None:
    """: `with_weekdays` keeps every listed row as it is, gives every other
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
