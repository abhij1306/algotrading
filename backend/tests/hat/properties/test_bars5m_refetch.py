"""Plain tests of the pure seams of the 5-minute refetch: which stored day is a target, when a
refetched day may replace it, and which gap rows a refetch leaves."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.hat.data.bars import Bar
from app.hat.data.bars5m_gaps import APPLIED, REFETCH_DIR, ROLLED_BACK, Gap, refetched_days
from app.hat.data.bars5m_refetch import (
    NO_FIRST_BAR,
    SHORT_CLOSE,
    admit,
    kept_outcomes,
    open_gaps,
    remaining_gaps,
    run_folder,
    short_spans,
    target_kind,
)
from app.hat.data.bars_basis import DailyRange
from app.hat.data.verify.bars5m import judge_opens, open_off_auction
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
    late = Bar("X", bars[0].start, Decimal("100.05"), Decimal(102), Decimal(99), Decimal(101), 100)
    assert admit(DAY, [late, *bars[1:]], DAILY, volume, TOL) == "open_not_auction"
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


def test_a_run_is_named_by_a_bare_folder_name_under_the_refetch_folder() -> None:
    root = Path("/data")
    assert run_folder(root, "run-20261030") == root / "bars5m_refetch" / "run-20261030"
    for name in ("", ".", "..", "../x", "a/b", "/abs", ".hidden", "a\\b"):
        with pytest.raises(ValueError, match="refused"):
            run_folder(root, name)


def test_a_first_open_is_the_auction_to_within_a_tick() -> None:
    assert not open_off_auction(1234.5, 1234.5)
    assert not open_off_auction(1234.51, 1234.5)
    assert open_off_auction(1234.55, 1234.5)
    assert open_off_auction(1213.6, 1234.5)


def test_an_open_off_the_auction_fails_unless_a_gap_row_refuses_it() -> None:
    day = date(2026, 7, 21)
    opens = {("a", day): (101.0, 100.0), ("b", day): (99.0, 100.0), ("c", day): (100.0, 100.0)}
    isins = {"a": "INE000A01011", "b": "INE000B01011", "c": "INE000C01011"}
    outside, inside = judge_opens(opens, isins, {"INE000B01011": ((day, day),)})
    assert [i["instrument_id"] for i in outside] == ["a"]
    assert [i["instrument_id"] for i in inside] == ["b"]
    assert outside[0] == {
        "instrument_id": "a",
        "isin": "INE000A01011",
        "day": "2026-07-21",
        "open_915": 101.0,
        "nse_open": 100.0,
    }


def test_open_gap_rows_are_one_per_session_named_by_the_run() -> None:
    days = [("INE000B01011", date(2026, 7, 23)), ("INE000A01011", date(2026, 7, 21))]
    rows = open_gaps([*days, days[0]], "run-1")
    assert [(g.vendor, g.isin, g.first, g.last) for g in rows] == [
        ("fyers", "INE000A01011", date(2026, 7, 21), date(2026, 7, 21)),
        ("fyers", "INE000B01011", date(2026, 7, 23), date(2026, 7, 23)),
    ]
    assert all(g.reason.startswith("open_not_auction: ") and "run-1" in g.reason for g in rows)


def test_a_resumed_fetch_keeps_only_the_finished_instruments_outcomes() -> None:
    rows = [{"instrument_id": "a", "outcome": "admitted"}, {"instrument_id": "b", "outcome": "x"}]
    assert kept_outcomes(rows, {"a"}) == rows[:1]
    assert kept_outcomes(rows, set()) == []


def test_a_rolled_back_run_wrote_no_fyers_days(tmp_path: Path) -> None:
    for name in ("run-a", "run-b"):
        folder = tmp_path / REFETCH_DIR / name
        folder.mkdir(parents=True)
        (folder / APPLIED).write_text(f"instrument_id,day\n{name},2026-07-21\n", encoding="utf-8")
    (tmp_path / REFETCH_DIR / "run-b" / ROLLED_BACK).write_text("restored\n", encoding="utf-8")
    assert refetched_days(tmp_path) == {("run-a", date(2026, 7, 21))}
