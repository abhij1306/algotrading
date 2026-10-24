"""PBT for initial stop rules, NSE tick sizes and
stop pricing at the broker."""

from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st
from pydantic import ValidationError

from app.hat.data.ticks import reference_close, tick_for_date, tick_size
from app.hat.engine import stops
from app.hat.engine.entries import ENTRY_SKIP_REASONS
from app.hat.engine.stops import StopInputs, plan_stop
from app.hat.engine.strategy.config import (
    StopAtrMultiple,
    StopBelowFinalContractionLow,
    StopLowOfDay,
    StopPctBelowEntry,
    StopSwingLow,
)
from app.hat.execution.gtt import GttPriceError, gtt_limit_price

ticks = st.sampled_from(
    [Decimal("0.01"), Decimal("0.05"), Decimal("0.1"), Decimal("1"), Decimal("5")]
)
triggers = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("500000"), places=2)


@given(triggers, ticks)
def test_gtt_limit_offset(trigger: Decimal, tick: Decimal) -> None:
    """For every trigger T > 0 and tick, the limit is T x 0.99 rounded down to the
    tick and below T; when that is not a positive price the stop is refused, not guessed."""
    try:
        limit = gtt_limit_price(trigger, tick)
    except GttPriceError:
        assert (trigger * Decimal("0.99") // tick) * tick <= 0
        return
    exact = trigger * Decimal("0.99")
    assert limit % tick == 0
    assert limit <= exact < limit + tick  # the largest tick multiple not above T x 0.99
    assert 0 < limit < trigger


@given(st.one_of(st.just(Decimal(0)), st.decimals(max_value=Decimal(0), allow_nan=False)), ticks)
def test_gtt_limit_refuses_a_non_positive_trigger(trigger: Decimal, tick: Decimal) -> None:
    with pytest.raises(GttPriceError):
        gtt_limit_price(trigger, tick)


TICK = Decimal("0.05")
prices = st.integers(1, 2_000_000).map(lambda n: Decimal(n) / 100)  # 0.01 .. 20000.00
pcts = st.integers(0, 5000).map(lambda n: Decimal(n) / 100)


# A low within 20% of a 100.00 entry, so `min_pct` decides often; `entry` draws other prices too.
near_entry_lows = st.integers(8000, 10_000).map(lambda n: Decimal(n) / 100)


def grid_floor(x: Fraction, tick: Decimal) -> Fraction:
    t = Fraction(tick)
    return (x // t) * t


@example(
    entry=Decimal(100), low=Decimal(98), buffer=Decimal(0), max_pct=Decimal(8), min_pct=Decimal(2)
)  # exactly min_pct below the entry is taken
@example(
    entry=Decimal(100), low=Decimal(99), buffer=Decimal(0), max_pct=Decimal(8), min_pct=Decimal(2)
)  # 1% below is too narrow
@given(
    entry=st.one_of(st.just(Decimal(100)), prices),
    low=st.one_of(prices, near_entry_lows),
    buffer=pcts,
    max_pct=st.integers(1, 10_000).map(lambda n: Decimal(n) / 100),
    min_pct=st.one_of(st.just(Decimal(0)), st.integers(0, 2000).map(lambda n: Decimal(n) / 100)),
)
def test_final_contraction_low(
    entry: Decimal, low: Decimal, buffer: Decimal, max_pct: Decimal, min_pct: Decimal
) -> None:
    """Stop = low x (1 - buffer/100) rounded down to the tick; it is skipped when
    it is not below the entry, more than max_pct percent below it, or, with min_pct above 0,
    less than min_pct percent below it (`stop_too_narrow`; min_pct 0 is off); a taken stop is on
    the tick grid, below the entry, and within [min_pct, max_pct]; every skip reason is one the
    signal stores as itself (`ENTRY_SKIP_REASONS`). The configured rule gives the same plan
    through `plan_stop`, and a rule with min_pct >= max_pct is refused. Harm: a stop wider than
    the rule allows risks more than the sizing assumed; one tighter than the base allows is
    shaken out by normal noise."""
    plan = stops.final_contraction_low(entry, low, buffer, max_pct, TICK, min_pct)
    exact = grid_floor(Fraction(low) * (100 - Fraction(buffer)) / 100, TICK)
    width = (Fraction(entry) - exact) * 100
    if not 0 < exact < Fraction(entry):
        assert plan.skip == stops.SKIP_NOT_BELOW_ENTRY
    elif width > Fraction(max_pct) * Fraction(entry):
        assert plan.skip == stops.SKIP_TOO_WIDE
    elif min_pct > 0 and width < Fraction(min_pct) * Fraction(entry):
        assert plan.skip == stops.SKIP_TOO_NARROW
    else:
        assert plan.stop is not None
        assert Fraction(plan.stop) == exact
        assert plan.stop % TICK == 0
        assert 0 < plan.stop < entry
        assert min_pct <= (entry - plan.stop) / entry * 100 <= max_pct
    assert plan.skipped == (plan.stop is None)
    assert plan.skipped == (plan.skip is not None)
    assert plan.skip is None or plan.skip in ENTRY_SKIP_REASONS  # stored as itself, not invalid
    if buffer > 50 or max_pct > 100:
        return
    fields = {"buffer_pct": float(buffer), "max_pct": float(max_pct), "min_pct": float(min_pct)}
    if min_pct >= max_pct:
        with pytest.raises(ValidationError):
            StopBelowFinalContractionLow(rule="below_final_contraction_low", **fields)
        return
    rule = StopBelowFinalContractionLow(rule="below_final_contraction_low", **fields)
    assert plan_stop(rule, StopInputs(entry, TICK, final_contraction_low=low)) == plan


@example(pivot=Decimal(100), above=100, low=Decimal("99.2"), max_pct=8, min_pct=2)
@given(
    pivot=st.one_of(st.just(Decimal(100)), prices),
    above=st.integers(0, 1500),
    low=st.one_of(prices, near_entry_lows),
    max_pct=st.integers(1, 30),
    min_pct=st.sampled_from([0, 0, 1, 2, 5]),
)
def test_contraction_stop_at_the_fill(
    pivot: Decimal, above: int, low: Decimal, max_pct: int, min_pct: int
) -> None:
    """The contraction stop the scan plans at the pivot, re-checked at a fill at
    or above it (the backtest's `check_width`), is the live plan at that fill, for every
    min_pct: a stop too narrow at the pivot is not dropped overnight, since a higher entry only
    widens it. Harm: the backtest skips (as `stop_input_missing`) trades live would take, so
    its results are not the live strategy's."""
    assume(low < pivot and min_pct < max_pct)
    entry = pivot + pivot * above / 10_000
    rule = StopBelowFinalContractionLow(
        rule="below_final_contraction_low", max_pct=float(max_pct), min_pct=float(min_pct)
    )
    live = plan_stop(rule, StopInputs(entry, TICK, final_contraction_low=low))
    planned = stops.plan_stop_at_pivot(rule, StopInputs(pivot, TICK, final_contraction_low=low))
    if planned.stop is None:
        assert live.stop is None
        return
    assert stops.check_width(entry, planned.stop, Decimal(max_pct), Decimal(min_pct)) == live


@given(prices, st.lists(prices, min_size=1, max_size=20), st.integers(0, 20))
def test_low_of_day(entry: Decimal, lows: list[Decimal], buffer_ticks: int) -> None:
    """Stop = the lowest low of the day so far less buffer_ticks ticks, rounded
    down to the tick (the lows here are on a 0.01 grid, the tick is 0.05); strictly below the
    entry or skipped. Harm: a stop above the day's low is hit by normal noise; one at or above
    the entry is not a stop; one off the tick grid is refused by the exchange."""
    plan = stops.low_of_day(entry, lows, buffer_ticks, TICK)
    exact = grid_floor(Fraction(min(lows) - buffer_ticks * TICK), TICK)
    if 0 < exact < Fraction(entry):
        assert plan.stop is not None
        assert Fraction(plan.stop) == exact
        assert plan.stop % TICK == 0
    else:
        assert plan.stop is None
        assert plan.skip == stops.SKIP_NOT_BELOW_ENTRY
    assert stops.low_of_day(entry, [], buffer_ticks, TICK).skip == stops.SKIP_BAD_INPUT


@given(
    prices,
    st.integers(1, 9999).map(lambda n: Decimal(n) / 100),
    st.integers(1, 500).map(lambda n: Decimal(n) / 10),
    st.lists(prices, min_size=1, max_size=20),
    st.sampled_from([Decimal("0.01"), Decimal("0.05"), Decimal("0.1"), Decimal(1)]),
)
def test_custom_rules(
    entry: Decimal, pct: Decimal, multiple: Decimal, lows: list[Decimal], tick: Decimal
) -> None:
    """Pct_below_entry, atr_multiple and swing_low give the formula
    rounded down to the tick, strictly below the entry, else a skip with a fixed reason; a rule
    whose input is missing is a skip, never a guess; the dispatcher gives what each function
    gives.
    Harm: a stop above or at the entry, or off the tick grid, is refused or fires at once."""
    atr = min(lows)
    expected = {
        "pct": grid_floor(Fraction(entry) * (100 - Fraction(pct)) / 100, tick),
        "atr": grid_floor(Fraction(entry) - Fraction(multiple) * Fraction(atr), tick),
        "swing": grid_floor(Fraction(min(lows)), tick),
    }
    plans = {
        "pct": stops.pct_below_entry(entry, pct, tick),
        "atr": stops.atr_multiple(entry, atr, multiple, tick),
        "swing": stops.swing_low(entry, lows, tick),
    }
    for name, plan in plans.items():
        if 0 < expected[name] < Fraction(entry):
            assert plan.stop is not None
            assert Fraction(plan.stop) == expected[name]
            assert plan.stop % tick == 0
        else:
            assert plan.stop is None
            assert plan.skip == stops.SKIP_NOT_BELOW_ENTRY
    inputs = StopInputs(entry, tick, atr=atr, swing_lows=lows)
    pct_rule = StopPctBelowEntry(rule="pct_below_entry", pct=float(pct))
    assert plan_stop(pct_rule, inputs) == plans["pct"]
    assert plan_stop(StopAtrMultiple(rule="atr_multiple", days=14, multiple=2.0), inputs) == (
        stops.atr_multiple(entry, atr, Decimal("2.0"), tick)
    )
    assert plan_stop(StopSwingLow(rule="swing_low", lookback_days=10), inputs) == plans["swing"]
    bare = StopInputs(entry, tick)
    for rule in (
        StopAtrMultiple(rule="atr_multiple", days=14, multiple=2.0),
        StopSwingLow(rule="swing_low", lookback_days=10),
        StopLowOfDay(rule="low_of_day", buffer_ticks=1),
        StopBelowFinalContractionLow(rule="below_final_contraction_low"),
    ):
        assert plan_stop(rule, bare).skip == stops.SKIP_BAD_INPUT


# --- tick sizes ----------------------------------------------------------------

# The NSE slabs since 15 April 2025, from the circular: (lowest price of the slab in paise, tick).
NSE_SLABS_PAISE = [
    (1, "0.01"),
    (25_000, "0.05"),
    (100_001, "0.10"),
    (500_001, "0.50"),
    (1_000_001, "1.00"),
    (2_000_001, "5.00"),
]
SLAB_EDGES = [p for lo, _ in NSE_SLABS_PAISE for p in (lo - 1, lo, lo + 1) if p > 0]


def oracle_tick(price_paise: int) -> Decimal:
    return Decimal([tick for lo, tick in NSE_SLABS_PAISE if price_paise >= lo][-1])


@st.composite
def close_histories(draw: st.DrawFn) -> tuple[list[date], list[Decimal | None], date]:
    trade = date(2026, draw(st.integers(1, 12)), draw(st.integers(1, 28)))
    days = draw(st.lists(st.integers(-70, 5), unique=True, max_size=12))
    dates = [trade + timedelta(days=d) for d in days]
    closes = [
        draw(
            st.one_of(
                st.none(),
                st.sampled_from(SLAB_EDGES + [10_000, 300_000]).map(
                    lambda p: Decimal(p).scaleb(-2)
                ),
            )
        )
        for _ in dates
    ]
    return dates, closes, trade


def test_sma() -> None:
    """The mean of the last n closes; None with fewer than n."""
    closes = [Decimal("10"), Decimal("11"), Decimal("13"), Decimal("12")]
    assert stops.sma(closes, 2) == Decimal("12.5")
    assert stops.sma(closes, 4) == Decimal("11.5")
    assert stops.sma(closes, 5) is None
    assert stops.sma([], 1) is None


def test_lowest() -> None:
    """The lowest of the last n lows; None with fewer than n."""
    lows = [Decimal("9"), Decimal("12"), Decimal("11"), Decimal("13")]
    assert stops.lowest(lows, 3) == Decimal("11")
    assert stops.lowest(lows, 4) == Decimal("9")
    assert stops.lowest(lows, 5) is None
    assert stops.lowest([], 1) is None


@example(price=24_999)
@example(price=25_000)  # exactly ₹250 is in the 0.05 slab
@example(price=100_000)  # exactly ₹1,000 is still 0.05
@example(price=100_001)
@given(price=st.one_of(st.sampled_from(SLAB_EDGES), st.integers(1, 10_000_000)))
def test_tick_size_slabs(price: int) -> None:
    """The tick of a price is the NSE slab it falls in (0.01 below ₹250, 0.05 to
    ₹1,000, 0.10 to ₹5,000, 0.50 to ₹10,000, 1.00 to ₹20,000, 5.00 above), each upper bound
    inclusive; a non-positive price has none. Harm: a stop or GTT price off the exchange's tick
    grid is refused by the broker, leaving a position with no stop."""
    assert tick_size(Decimal(price).scaleb(-2)) == oracle_tick(price)
    assert tick_size(Decimal(-price).scaleb(-2)) is None
    assert tick_size(Decimal(0)) is None


@given(history=close_histories())
def test_tick_size_reference_close(
    history: tuple[list[date], list[Decimal | None], date],
) -> None:
    """(part of `tick_size_slabs`): the tick of a trade date is set from the last
    close before the first day of its month, else (a listing this month) the latest close before
    the trade date; never from the trade date's own close or a later one. Harm: a stock that
    crossed ₹250 this month is priced on the new grid a month early and its stop is refused."""
    dates, closes, trade = history
    known = sorted((d, c) for d, c in zip(dates, closes, strict=True) if c is not None)
    before_month = [c for d, c in known if d < trade.replace(day=1)]
    before_trade = [c for d, c in known if d < trade]
    want = before_month[-1] if before_month else (before_trade[-1] if before_trade else None)
    assert reference_close(dates, closes, trade) == want
    tick = tick_for_date(dates, closes, trade)
    assert tick == (None if want is None else oracle_tick(int(want.scaleb(2))))


def test_history_inputs_at_each_rules_own_length() -> None:
    """Each stop rule reads the ATR or swing lows at its own length (`history_inputs`), so two
    bots whose ATR or swing lengths differ each get their own input; a swing lookback longer
    than the known lows gives none, and a rule reading neither gets neither."""
    highs = [Decimal(110 + n % 7) for n in range(30)]
    lows = [Decimal(100 + n % 5) for n in range(30)]
    closes = [Decimal(105 + n % 3) for n in range(30)]
    history = stops.SeriesHistory(highs, lows, closes)
    for days in (14, 20):
        rule = StopAtrMultiple(rule="atr_multiple", days=days, multiple=2.0)
        want = stops.mean_true_range(highs, lows, closes, days)
        assert want is not None
        assert stops.history_inputs(rule, history) == (want, ())
    assert stops.history_inputs(StopSwingLow(rule="swing_low", lookback_days=10), history) == (
        None,
        lows[-10:],
    )
    gappy = stops.SeriesHistory(highs, [*lows[:-1], None], closes)
    assert stops.history_inputs(StopSwingLow(rule="swing_low", lookback_days=10), gappy) == (
        None,
        (),
    )
    atr = StopAtrMultiple(rule="atr_multiple", days=14, multiple=2.0)
    assert stops.history_inputs(atr, gappy) == (None, ())
    assert stops.history_inputs(StopLowOfDay(rule="low_of_day"), history) == (None, ())
