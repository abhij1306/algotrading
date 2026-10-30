"""Plain tests of the pure seams of the 5-minute refetch: which stored day is a target, when a
refetched day may replace it, and which gap rows a refetch leaves."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from app.hat.data.bars import Bar
from app.hat.data.bars5m_gaps import Gap
from app.hat.data.bars5m_refetch import (
    NO_FIRST_BAR,
    SHORT_CLOSE,
    admit,
    remaining_gaps,
    short_spans,
    target_kind,
)
from app.hat.data.bars_basis import DailyRange
from app.hat.time import IST

DAY = date(2026, 9, 1)
TOL = Decimal("0.005")
DAILY = DailyRange(Decimal(100), Decimal(102), Decimal(99), Decimal(101))


def _session(first: time = time(9, 15), last: time = time(15, 25), volume: int = 100) -> list[Bar]:
    """Flat bars from `first` to `last` inside the daily range, `volume` each."""
    at = datetime.combine(DAY, first, tzinfo=IST)
    end = datetime.combine(DAY, last, tzinfo=IST)
    bars = []
    while at <= end:
        bars.append(Bar("X", at, Decimal(100), Decimal(101), Decimal(100), Decimal(101), volume))
        at += timedelta(minutes=5)
    bars[0] = Bar("X", bars[0].start, Decimal(100), Decimal(102), Decimal(99), Decimal(101), volume)
    return bars


def test_targets_are_short_gap_sessions_and_member_days_without_the_first_bar() -> None:
    assert target_kind(time(9, 15), time(15, 10), in_kotak_gap=True, member=False) == SHORT_CLOSE
    assert target_kind(time(9, 20), time(15, 10), in_kotak_gap=True, member=True) == SHORT_CLOSE
    assert target_kind(time(9, 15), time(15, 25), in_kotak_gap=True, member=True) is None
    assert target_kind(time(9, 20), time(15, 25), in_kotak_gap=False, member=True) == NO_FIRST_BAR
    assert target_kind(time(9, 20), time(15, 25), in_kotak_gap=False, member=False) is None
    assert target_kind(None, None, in_kotak_gap=True, member=True) is None


def test_a_whole_day_that_fits_is_admitted() -> None:
    bars = _session()
    assert admit(DAY, bars, DAILY, sum(b.volume for b in bars), TOL) is None


def test_a_refetched_day_is_refused_by_the_first_rule_it_breaks() -> None:
    bars = _session()
    volume = sum(b.volume for b in bars)
    assert admit(DAY, [], DAILY, volume, TOL) == "no_bars"
    assert admit(DAY, bars, DAILY, volume, TOL, screened=1) == "screened_bucket"
    assert admit(DAY, bars, None, volume, TOL) == "no_daily_bar"
    assert admit(DAY, bars[1:], DAILY, volume, TOL) == "basis_mismatch"  # the range is at 09:15
    assert admit(DAY, _session(last=time(15, 20)), DAILY, volume, TOL) == "no_last_bar"
    lost = datetime.combine(DAY, time(15, 30), tzinfo=IST) - timedelta(minutes=5)
    gapped = [b for b in bars if b.start != datetime.combine(DAY, time(12, 0), tzinfo=IST)]
    assert admit(DAY, gapped, DAILY, volume, TOL, refused={lost}) is None
    noon = datetime.combine(DAY, time(12, 0), tzinfo=IST)
    assert admit(DAY, gapped, DAILY, volume, TOL, refused={noon}) == "lacks_refused"
    assert admit(DAY, bars, DAILY, int(volume / Decimal("0.85")), TOL) == "day_volume_differs"
    assert admit(DAY, bars, DAILY, int(volume / Decimal("1.02")), TOL) == "day_volume_differs"
    assert admit(DAY, bars, DAILY, None, TOL) == "day_volume_differs"


def test_a_day_off_the_grid_or_far_from_the_daily_open_is_not_on_basis() -> None:
    bars = _session()
    volume = sum(b.volume for b in bars)
    odd = bars[0]
    shifted = Bar("X", odd.start + timedelta(minutes=1), odd.open, odd.high, odd.low, odd.close, 1)
    assert admit(DAY, [bars[0], shifted, *bars[1:]], DAILY, volume + 1, TOL) == "not_on_basis"
    far = DailyRange(Decimal(106), Decimal(102), Decimal(99), Decimal(101))
    assert admit(DAY, bars, far, volume, TOL) == "not_on_basis"


def test_short_spans_split_at_any_session_that_is_whole() -> None:
    days = [date(2026, 8, d) for d in (3, 4, 5, 6, 7, 10)]
    assert short_spans({days[0], days[1], days[3], days[5]}, days) == [
        (days[0], days[1]),
        (days[3], days[3]),
        (days[5], days[5]),
    ]
    assert short_spans(set(), days) == []


def test_a_kotak_row_is_dropped_when_whole_and_narrowed_when_not() -> None:
    days = [date(2026, 8, d) for d in (3, 4, 5)]
    first, last = days[0], days[-1]
    nse = Gap("nse", date(2024, 3, 2), date(2024, 3, 2), "*", "special session")
    whole = Gap("kotak", first, last, "INE000A01011", "cut at 15:10")
    short = Gap("kotak", first, last, "INE000B01011", "cut at 15:10")
    left = remaining_gaps(
        [nse, whole, short],
        {"INE000B01011": {days[1]}},
        {"INE000A01011": days, "INE000B01011": days},
        "still short",
    )
    assert left == [nse, Gap("kotak", days[1], days[1], "INE000B01011", "still short")]
