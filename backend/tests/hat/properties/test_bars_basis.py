"""Plain tests of the pure seams of the raw basis and the bad-candle policy (audit D1, D2)."""

from datetime import date, datetime, timedelta
from decimal import Decimal

from app.hat.data.bars import Bar
from app.hat.data.bars5m_repair import CHAIN_DUPLICATE, mend
from app.hat.data.bars_basis import (
    BASIS_MISMATCH,
    CONVERTED,
    KEPT,
    NO_DAILY_BAR,
    DailyRange,
    check_day,
    fyers_factor,
    mend_day,
    to_raw,
)
from app.hat.data.candles import parse_candles, screen
from app.hat.data.minute_history import _Loaded
from app.hat.data.minute_limits import HistoryLimits
from app.hat.time import IST

OPEN = datetime(2026, 10, 20, 9, 15, tzinfo=IST)
EPOCH = int(OPEN.timestamp())


def _bar(o: str, h: str, low: str, c: str, volume: int = 100, minutes: int = 0) -> Bar:
    start = OPEN + timedelta(minutes=minutes)
    return Bar("S", start, Decimal(o), Decimal(h), Decimal(low), Decimal(c), volume)


def test_fyers_factor_counts_actions_after_the_day_up_to_the_fetch() -> None:
    actions = [(date(2026, 3, 2), Decimal("0.2")), (date(2026, 9, 1), Decimal("0.5"))]
    assert fyers_factor(actions, date(2026, 1, 5), date(2026, 10, 22)) == Decimal("0.1")
    assert fyers_factor(actions, date(2026, 3, 2), date(2026, 10, 22)) == Decimal("0.5")
    assert fyers_factor(actions, date(2026, 1, 5), date(2026, 8, 31)) == Decimal("0.2")
    assert fyers_factor(actions, date(2026, 9, 1), date(2026, 10, 22)) == 1
    assert fyers_factor([], date(2026, 1, 5), date(2026, 10, 22)) == 1


def test_to_raw_divides_prices_and_multiplies_volume() -> None:
    raw = to_raw(_bar("20.1", "20.5", "19.9", "20.3", volume=5003), Decimal("0.2"))
    assert (raw.open, raw.high, raw.low, raw.close) == (
        Decimal("100.5000"),
        Decimal("102.5000"),
        Decimal("99.5000"),
        Decimal("101.5000"),
    )
    assert raw.volume == 1001  # 1000.6, half-even to whole shares
    same = _bar("1", "1", "1", "1")
    assert to_raw(same, Decimal(1)) is same


def test_check_day_envelope() -> None:
    bars = [_bar("100", "104", "99", "103"), _bar("103", "105", "101", "102", minutes=5)]
    tol = Decimal("0.005")
    assert check_day(bars, DailyRange(*map(Decimal, ("99", "105", "99", "101"))), tol) is None
    # the close is not compared with the daily close (closing auction), only with its range
    assert check_day(bars, DailyRange(*map(Decimal, ("99", "105.4", "99", "90"))), tol) is None
    assert check_day(bars, DailyRange(*map(Decimal, ("99", "106", "99", "101"))), tol) == (
        BASIS_MISMATCH
    )
    # a split basis: everything five times the raw daily bar
    assert check_day(bars, DailyRange(*map(Decimal, ("20", "21", "19.8", "20.4"))), tol) == (
        BASIS_MISMATCH
    )
    assert check_day(bars, None, tol) == NO_DAILY_BAR


def test_mend_day_converts_an_adjusted_day_and_never_a_fitting_one() -> None:
    tol = Decimal("0.005")
    raw = DailyRange(*map(Decimal, ("200", "210", "198", "204")))
    # stored on the basis of a later 1:1 bonus: half the raw prices
    adjusted = [_bar("100", "104", "99", "103"), _bar("103", "105", "101", "102", minutes=5)]
    verdict, bars = mend_day(adjusted, Decimal("0.5"), raw, tol)
    assert verdict == CONVERTED
    assert (bars[0].open, max(b.high for b in bars), bars[0].volume) == (200, 210, 50)
    # a day that fits as stored is kept as it is, whatever the factor: mending twice is a no-op
    assert mend_day(bars, Decimal("0.5"), raw, tol) == (KEPT, bars)
    # no factor, or a conversion that still misses, drops the day with the check's code
    assert mend_day(adjusted, Decimal(1), raw, tol) == (BASIS_MISMATCH, [])
    assert mend_day(adjusted, Decimal("0.25"), raw, tol) == (BASIS_MISMATCH, [])
    assert mend_day(adjusted, Decimal("0.5"), None, tol) == (NO_DAILY_BAR, [])


def test_mend_refiles_a_day_under_the_chain_member_with_its_daily_bar() -> None:
    tol, day = Decimal("0.005"), OPEN.date()
    bars = [_bar("100", "104", "99", "103")]
    fits = DailyRange(*map(Decimal, ("100", "104", "99", "103")))
    chain, daily = ("new", "old"), {("old", day): fits}
    done, out = mend("new", day, bars, chain, daily, {"new"}, Decimal(1), tol)
    assert done is not None
    assert (done.action, done.owner, done.code) == ("refiled", "old", None)
    assert [b.symbol for b in out] == ["old"]
    # the owner already holds the day: the misfiled copy is a duplicate
    done, out = mend("new", day, bars, chain, daily, {"new", "old"}, Decimal(1), tol)
    assert done is not None
    assert (done.action, done.code, out) == ("dropped", CHAIN_DUPLICATE, [])
    # no member has the daily bar
    done, out = mend("new", day, bars, chain, {}, {"new"}, Decimal(1), tol)
    assert done is not None
    assert (done.action, done.code) == ("dropped", NO_DAILY_BAR)
    # a day that fits under its own id is left alone
    assert mend("old", day, bars, chain, daily, {"old"}, Decimal("0.5"), tol) == (None, [])


def test_parse_candles_rejects_each_rule_with_its_code() -> None:
    good = [EPOCH, 10.0, 11.0, 9.5, 10.5, 7]
    rows = [
        good,
        [EPOCH + 60, 10.0, 11.0],  # shape, its epoch still readable
        [10**20, 10.0, 11.0, 9.5, 10.5, 7],  # epoch
        [EPOCH + 120, 10.0, 11.0, 9.5, 10.5, -1],  # volume
        [EPOCH + 180, "10", 11.0, 9.5, 10.5, 7],  # price
        [EPOCH + 240, 10.0, 9.0, 9.5, 10.5, 7],  # ohlc
        [EPOCH + 300, 10.0, 11.0, 9.5, 10.5, 7.0],
    ]
    parsed = parse_candles(rows)
    assert [r.rule for r in parsed.rejected] == ["shape", "epoch", "volume", "price", "ohlc"]
    assert [c.at.timestamp() for c in parsed.candles] == [EPOCH, EPOCH + 300]
    assert parsed.longest_run == 5
    unplaced = parsed.rejected[1]
    assert unplaced.at is None
    assert {v.timestamp() for v in unplaced.voids} == {EPOCH + 60, EPOCH + 120}  # its neighbours


def test_screen_drops_the_whole_bucket_of_a_rejected_candle() -> None:
    rows = [
        [EPOCH, 10.0, 11.0, 9.5, 10.5, 7],
        [EPOCH + 60, 10.0, 9.0, 9.5, 10.5, 7],  # ohlc: the 09:15 bucket goes
        [EPOCH + 300, 10.0, 11.0, 9.5, 10.5, 7],  # 09:20 stays
    ]
    kept = screen(parse_candles(rows))
    assert [c.at.timestamp() for c in kept] == [EPOCH + 300]


def test_reject_threshold() -> None:
    limits = HistoryLimits(max_reject_pct=Decimal("0.5"), max_reject_run=3)
    rejected = parse_candles([["x"]]).rejected
    assert not _Loaded(date(2026, 1, 1), rejected=rejected, candles=200).too_many_rejects(limits)
    assert _Loaded(date(2026, 1, 1), rejected=rejected, candles=199).too_many_rejects(limits)
    run = _Loaded(date(2026, 1, 1), rejected=rejected, candles=10_000, longest_run=3)
    assert run.too_many_rejects(limits)


def test_reject_run_carries_across_answers() -> None:
    first = parse_candles([[EPOCH, 10.0, 11.0, 9.5, 10.5, 7], ["x"], ["x"]])
    assert (first.longest_run, first.trailing_run) == (2, 2)
    second = parse_candles([["x"], [EPOCH + 600, 10.0, 11.0, 9.5, 10.5, 7]], first.trailing_run)
    assert (second.longest_run, second.trailing_run) == (3, 0)
