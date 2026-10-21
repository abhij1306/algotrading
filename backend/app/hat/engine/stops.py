"""Initial stop rules (07 §5, HAT-EXEC-050..052, HAT-BOT-062): pure and exact.

Every rule returns a `StopPlan`: a stop price, or a skip with a fixed reason. A stop is valid only
when it is strictly between 0 and the entry price (HAT-RISK-073), and every stop is rounded down
to the tick grid (a lower stop is never tighter than the formula, and an off-grid price is
refused by the exchange). The initial stop is computed before the entry is sized; a skip means no
signal is taken, and the reason reaches the daily digest (HAT-EXEC-043).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from .strategy.config import (
    StopAtrMultiple,
    StopBelowFinalContractionLow,
    StopLowOfDay,
    StopPctBelowEntry,
    StopRule,
    StopSwingLow,
)

__all__ = [
    "DEFAULT_TICK",
    "SKIP_BAD_INPUT",
    "SKIP_NOT_BELOW_ENTRY",
    "SKIP_TOO_WIDE",
    "StopInputs",
    "StopPlan",
    "atr_multiple",
    "check_width",
    "final_contraction_low",
    "floor_to_tick",
    "low_of_day",
    "pct_below_entry",
    "plan_stop",
    "swing_low",
]

# [A] the NSE tick of most equities; the real tick comes from the instrument in PR 15.
DEFAULT_TICK = Decimal("0.05")
SKIP_TOO_WIDE = "stop_too_wide"
SKIP_NOT_BELOW_ENTRY = "stop_not_below_entry"
SKIP_BAD_INPUT = "stop_input_missing"
_HUNDRED = Decimal(100)


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


def check_width(entry: Decimal, stop: Decimal, max_pct: Decimal) -> StopPlan:
    """`stop` itself, or a skip when it is not below the entry or (entry - stop) / entry is more
    than `max_pct` percent."""
    plan = _valid(entry, stop)
    if plan.skipped:
        return plan
    if (entry - stop) * _HUNDRED > max_pct * entry:
        return _skip(SKIP_TOO_WIDE)
    return plan


def final_contraction_low(
    entry: Decimal, low: Decimal, buffer_pct: Decimal, max_pct: Decimal, tick: Decimal
) -> StopPlan:
    """HAT-EXEC-050: stop = low x (1 - buffer/100) rounded down to the tick; skipped when it is
    more than `max_pct` percent below the entry."""
    if not (_positive(entry, low, tick) and isinstance(buffer_pct, Decimal) and buffer_pct >= 0):
        return _skip(SKIP_BAD_INPUT)
    stop = floor_to_tick(low * (_HUNDRED - buffer_pct) / _HUNDRED, tick)
    return check_width(entry, stop, max_pct)


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
    """What a stop rule may read at entry: the entry price, the tick, and whichever market
    values the rule needs (a missing one is a skip, never a guess)."""

    entry: Decimal
    tick: Decimal = DEFAULT_TICK
    final_contraction_low: Decimal | None = None
    day_lows: Sequence[Decimal] = ()
    atr: Decimal | None = None
    swing_lows: Sequence[Decimal] = ()


def _dec(value: float) -> Decimal:
    return Decimal(repr(value))


def plan_stop(rule: StopRule, inputs: StopInputs) -> StopPlan:
    """The initial stop of one configured rule for one entry."""
    entry, tick = inputs.entry, inputs.tick
    if isinstance(rule, StopBelowFinalContractionLow):
        if inputs.final_contraction_low is None:
            return _skip(SKIP_BAD_INPUT)
        return final_contraction_low(
            entry, inputs.final_contraction_low, _dec(rule.buffer_pct), _dec(rule.max_pct), tick
        )
    if isinstance(rule, StopLowOfDay):
        return low_of_day(entry, inputs.day_lows, rule.buffer_ticks, tick)
    if isinstance(rule, StopPctBelowEntry):
        return pct_below_entry(entry, _dec(rule.pct), tick)
    if isinstance(rule, StopAtrMultiple):
        if inputs.atr is None:
            return _skip(SKIP_BAD_INPUT)
        return atr_multiple(entry, inputs.atr, _dec(rule.multiple), tick)
    if isinstance(rule, StopSwingLow):
        return swing_low(entry, inputs.swing_lows, tick)
    return _skip(SKIP_BAD_INPUT)
