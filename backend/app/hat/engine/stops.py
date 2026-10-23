"""Initial stop rules (HAT-EXEC-050..052, HAT-BOT-062): pure and exact.

Every rule returns a `StopPlan`: a stop price, or a skip with a fixed reason. A stop is valid only
when it is strictly between 0 and the entry price (HAT-RISK-073), and every stop is rounded down
to the tick grid (a lower stop is never tighter than the formula, and an off-grid price is
refused by the exchange). The initial stop is computed before the entry is sized; a skip means no
signal is taken, and the reason reaches the daily digest (HAT-EXEC-043).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from ..money import require_exact
from .strategy.config import (
    StopAtrMultiple,
    StopBelowFinalContractionLow,
    StopLowOfDay,
    StopPctBelowEntry,
    StopRule,
    StopSwingLow,
)

__all__ = [
    "SKIP_BAD_INPUT",
    "SKIP_NOT_BELOW_ENTRY",
    "SKIP_TOO_NARROW",
    "SKIP_TOO_WIDE",
    "StopInputs",
    "StopPlan",
    "atr_multiple",
    "check_width",
    "final_contraction_low",
    "floor_to_tick",
    "low_of_day",
    "lowest",
    "mean_true_range",
    "pct_below_entry",
    "plan_stop",
    "plan_stop_at_pivot",
    "sma",
    "swing_low",
]

SKIP_TOO_WIDE = "stop_too_wide"
SKIP_TOO_NARROW = "stop_too_narrow"
SKIP_NOT_BELOW_ENTRY = "stop_not_below_entry"
SKIP_BAD_INPUT = "stop_input_missing"
_HUNDRED = Decimal(100)
_ZERO = Decimal(0)


@dataclass(frozen=True)
class StopPlan:
    """`stop` is the price, or None with the `skip` reason."""

    stop: Decimal | None
    skip: str | None = None

    @property
    def skipped(self) -> bool:
        return self.stop is None


def _skip(reason: str) -> StopPlan:
    return StopPlan(None, reason)


def _positive(*values: object) -> bool:
    return all(isinstance(v, Decimal) and v.is_finite() and v > 0 for v in values)


def floor_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    """The largest multiple of `tick` not above `value`."""
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def _valid(entry: Decimal, stop: Decimal) -> StopPlan:
    if not 0 < stop < entry:
        return _skip(SKIP_NOT_BELOW_ENTRY)
    return StopPlan(stop)


def check_width(
    entry: Decimal, stop: Decimal, max_pct: Decimal, min_pct: Decimal = _ZERO
) -> StopPlan:
    """`stop` itself, or a skip when it is not below the entry, when (entry - stop) / entry is
    more than `max_pct` percent, or, with `min_pct` above 0, less than `min_pct` percent."""
    plan = _valid(entry, stop)
    if plan.skipped:
        return plan
    width = (entry - stop) * _HUNDRED
    if width > max_pct * entry:
        return _skip(SKIP_TOO_WIDE)
    if min_pct > 0 and width < min_pct * entry:
        return _skip(SKIP_TOO_NARROW)
    return plan


def final_contraction_low(
    entry: Decimal,
    low: Decimal,
    buffer_pct: Decimal,
    max_pct: Decimal,
    tick: Decimal,
    min_pct: Decimal = _ZERO,
) -> StopPlan:
    """HAT-EXEC-050: stop = low x (1 - buffer/100) rounded down to the tick; skipped when it is
    more than `max_pct` percent below the entry, or less than `min_pct` percent (0 = off)."""
    if not (_positive(entry, low, tick) and isinstance(buffer_pct, Decimal) and buffer_pct >= 0):
        return _skip(SKIP_BAD_INPUT)
    stop = floor_to_tick(low * (_HUNDRED - buffer_pct) / _HUNDRED, tick)
    return check_width(entry, stop, max_pct, min_pct)


def low_of_day(
    entry: Decimal, day_lows: Sequence[Decimal], buffer_ticks: int, tick: Decimal
) -> StopPlan:
    """HAT-EXEC-051: the lowest low of the day's bars up to the entry bar, less `buffer_ticks`
    ticks, rounded down to the tick (a low off the grid gives no off-grid stop); it must be
    strictly below the entry."""
    if not day_lows or not _positive(entry, tick, *day_lows) or buffer_ticks < 0:
        return _skip(SKIP_BAD_INPUT)
    return _valid(entry, floor_to_tick(min(day_lows) - buffer_ticks * tick, tick))


def pct_below_entry(entry: Decimal, pct: Decimal, tick: Decimal) -> StopPlan:
    """HAT-EXEC-052: entry x (1 - pct/100), rounded down to the tick."""
    if not (_positive(entry, tick) and isinstance(pct, Decimal) and pct > 0):
        return _skip(SKIP_BAD_INPUT)
    return _valid(entry, floor_to_tick(entry * (_HUNDRED - pct) / _HUNDRED, tick))


def mean_true_range(
    highs: Sequence[Decimal], lows: Sequence[Decimal], closes: Sequence[Decimal], length: int
) -> Decimal | None:
    """The mean true range of the last `length` sessions of these daily bars (oldest first; the
    session before them gives the first previous close), or None with fewer than `length` + 1
    bars. The live stop and the backtest both read the ATR from here."""
    if length < 1 or len(closes) < length + 1:
        return None
    start = len(closes) - length
    ranges = (
        max(highs[i], closes[i - 1]) - min(lows[i], closes[i - 1])
        for i in range(start, len(closes))
    )
    return sum(ranges, _ZERO) / length


def sma(closes: Sequence[Decimal], length: int) -> Decimal | None:
    """The mean of the last `length` closes (oldest first), or None with fewer than `length`.
    The close-phase exits of the live loop and the backtest both read the SMA from here."""
    if length < 1 or len(closes) < length:
        return None
    return sum(closes[-length:], _ZERO) / length


def lowest(lows: Sequence[Decimal], length: int) -> Decimal | None:
    """The lowest of the last `length` lows (oldest first), or None with fewer than `length`.
    The trailing swing-low exit of the live loop and the backtest both read it from here."""
    if length < 1 or len(lows) < length:
        return None
    return min(lows[-length:])


def atr_multiple(entry: Decimal, atr: Decimal, multiple: Decimal, tick: Decimal) -> StopPlan:
    """HAT-EXEC-052: entry - multiple x ATR, rounded down to the tick."""
    if not _positive(entry, atr, multiple, tick):
        return _skip(SKIP_BAD_INPUT)
    return _valid(entry, floor_to_tick(entry - multiple * atr, tick))


def swing_low(entry: Decimal, lows: Sequence[Decimal], tick: Decimal) -> StopPlan:
    """HAT-EXEC-052: the lowest low over the lookback, rounded down to the tick."""
    if not lows or not _positive(entry, tick, *lows):
        return _skip(SKIP_BAD_INPUT)
    return _valid(entry, floor_to_tick(min(lows), tick))


@dataclass(frozen=True)
class StopInputs:
    """What a stop rule may read at entry: the entry price, the instrument's tick on the trade
    date (`data.ticks`, HAT-EXEC-053; there is no default tick), and whichever market values the
    rule needs (a missing one is a skip, never a guess)."""

    entry: Decimal
    tick: Decimal
    final_contraction_low: Decimal | None = None
    day_lows: Sequence[Decimal] = ()
    atr: Decimal | None = None
    swing_lows: Sequence[Decimal] = ()


def plan_stop_at_pivot(rule: StopRule, inputs: StopInputs) -> StopPlan:
    """The stop planned overnight with the pivot as the entry (`inputs.entry`). The contraction
    rule's `min_pct` is left to the entry: a contraction stop does not move with the entry, so
    a higher entry only widens it, and a stop too narrow at the pivot can be wide enough at the
    fill (`check_width` at the entry applies it, as the live plan does)."""
    if isinstance(rule, StopBelowFinalContractionLow) and rule.min_pct:
        rule = rule.model_copy(update={"min_pct": 0.0})
    return plan_stop(rule, inputs)


def _contraction(rule: StopBelowFinalContractionLow, inputs: StopInputs) -> StopPlan:
    if inputs.final_contraction_low is None:
        return _skip(SKIP_BAD_INPUT)
    return final_contraction_low(
        inputs.entry,
        inputs.final_contraction_low,
        require_exact(rule.buffer_pct, "buffer_pct", floats=True),
        require_exact(rule.max_pct, "max_pct", floats=True),
        inputs.tick,
        require_exact(rule.min_pct, "min_pct", floats=True),
    )


def _low_of_day(rule: StopLowOfDay, inputs: StopInputs) -> StopPlan:
    return low_of_day(inputs.entry, inputs.day_lows, rule.buffer_ticks, inputs.tick)


def _pct_below(rule: StopPctBelowEntry, inputs: StopInputs) -> StopPlan:
    return pct_below_entry(inputs.entry, require_exact(rule.pct, "pct", floats=True), inputs.tick)


def _atr_multiple(rule: StopAtrMultiple, inputs: StopInputs) -> StopPlan:
    if inputs.atr is None:
        return _skip(SKIP_BAD_INPUT)
    multiple = require_exact(rule.multiple, "multiple", floats=True)
    return atr_multiple(inputs.entry, inputs.atr, multiple, inputs.tick)


def _swing_low(rule: StopSwingLow, inputs: StopInputs) -> StopPlan:
    return swing_low(inputs.entry, inputs.swing_lows, inputs.tick)


# The planner of each stop rule; an unknown rule is a skip.
_PLANNERS: dict[type, Callable[[Any, StopInputs], StopPlan]] = {
    StopBelowFinalContractionLow: _contraction,
    StopLowOfDay: _low_of_day,
    StopPctBelowEntry: _pct_below,
    StopAtrMultiple: _atr_multiple,
    StopSwingLow: _swing_low,
}


def plan_stop(rule: StopRule, inputs: StopInputs) -> StopPlan:
    """The initial stop of one configured rule for one entry."""
    planner = _PLANNERS.get(type(rule))
    return _skip(SKIP_BAD_INPUT) if planner is None else planner(rule, inputs)
