"""PBT for initial stop rules (HAT-EXEC-050..052, HAT-BOT-062) and stop pricing at the broker
(HAT-EXEC-062)."""

from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.hat.engine import stops
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
    """HAT-EXEC-062: for every trigger T > 0 and tick, the limit is T x 0.99 rounded down to the
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


def grid_floor(x: Fraction, tick: Decimal) -> Fraction:
    t = Fraction(tick)
    return (x // t) * t


@given(prices, prices, pcts, st.integers(1, 10_000).map(lambda n: Decimal(n) / 100))
def test_final_contraction_low(
    entry: Decimal, low: Decimal, buffer: Decimal, max_pct: Decimal
) -> None:
    """HAT-EXEC-050: stop = low x (1 - buffer/100) rounded down to the tick; it is skipped when
    it is not below the entry or more than max_pct percent below it; a taken stop is on the tick
    grid, below the entry, and never wider than max_pct. Harm: a stop wider than the rule allows
    risks more than the sizing assumed."""
    plan = stops.final_contraction_low(entry, low, buffer, max_pct, TICK)
    exact = grid_floor(Fraction(low) * (100 - Fraction(buffer)) / 100, TICK)
    if not 0 < exact < Fraction(entry):
        assert plan.skip == stops.SKIP_NOT_BELOW_ENTRY
    elif (Fraction(entry) - exact) * 100 > Fraction(max_pct) * Fraction(entry):
        assert plan.skip == stops.SKIP_TOO_WIDE
    else:
        assert plan.stop is not None
        assert Fraction(plan.stop) == exact
        assert plan.stop % TICK == 0
        assert 0 < plan.stop < entry
        assert (entry - plan.stop) / entry * 100 <= max_pct
    assert plan.skipped == (plan.stop is None)
    assert plan.skipped == (plan.skip is not None)


@given(prices, st.lists(prices, min_size=1, max_size=20), st.integers(0, 20))
def test_low_of_day(entry: Decimal, lows: list[Decimal], buffer_ticks: int) -> None:
    """HAT-EXEC-051: stop = the lowest low of the day so far less buffer_ticks ticks, rounded
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
    """HAT-EXEC-052, HAT-BOT-062: pct_below_entry, atr_multiple and swing_low give the formula
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
