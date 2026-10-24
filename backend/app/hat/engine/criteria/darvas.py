"""`darvas_box`: Nicolas Darvas's box at a new 52-week high (HAT-CRIT-124).

Darvas (How I Made $2,000,000 in the Stock Market, 1960) bought a stock making new highs once it
settled into a "box": the top is a high the next sessions do not exceed, the bottom the low formed
after it that the next sessions do not undercut, each confirmed by `confirm_days` sessions (three
in the usual codification of his rules). The box top is the breakout level and the stop goes just
under the box bottom. Here the top is the highest high of the `lookback_days` window, so the box
is the one at the 52-week high and a break of it is a new high.

The criterion exposes the top as `pivot` and the bottom as `final_contraction_low`, the levels the
trigger (`price_breaks_level: pivot`) and the stop rule (`below_final_contraction_low`) read.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from pydantic import Field, model_validator

from .base import REASON_OK, CriterionResult, EvalContext, ParamModel, dec, no_data, verdict
from .indicators import last

WINDOW_52W = 252


class DarvasBoxParams(ParamModel):
    """`max_box_days` bounds the box's age (the top's session to the date, inclusive), so the high
    is recent; `max_depth_pct` bounds (top - bottom) / top, so the stop is not too wide."""

    lookback_days: int = Field(default=WINDOW_52W, ge=5, le=WINDOW_52W)
    confirm_days: int = Field(default=3, ge=1, le=20)
    max_box_days: int = Field(default=60, ge=3, le=WINDOW_52W)
    max_depth_pct: float = Field(default=8.0, gt=0, le=100)

    @model_validator(mode="after")
    def _check(self) -> DarvasBoxParams:
        # the shortest box: the top, `confirm_days` sessions to the bottom, `confirm_days` after it
        if not self.confirm_days + 2 <= self.max_box_days <= self.lookback_days:
            raise ValueError("max_box_days is confirm_days + 2 to lookback_days")
        return self


@dataclass(frozen=True)
class Box:
    """The box's top and bottom prices and the top's age in sessions (1 = the last bar)."""

    top: Decimal
    bottom: Decimal
    age: int


def find_box(highs: Sequence[float], lows: Sequence[float], confirm_days: int) -> Box | str:
    """The box of a window of bars (oldest first, no gaps), or the reason there is none. The top
    is the window's highest high (its first session on a tie) with at least `confirm_days`
    sessions after it; the bottom is the lowest low after the top (its first session on a tie)
    with at least `confirm_days` sessions after it, and strictly below the top."""
    tops = [dec(h) for h in highs]
    top = max(tops, default=None)
    if top is None or len(lows) != len(tops):
        return "no_box"
    start = tops.index(top)
    if len(tops) - 1 - start < confirm_days:
        return "box_top_unconfirmed"
    after = [dec(v) for v in lows[start + 1 :]]
    bottom = min(after)
    if len(after) - 1 - after.index(bottom) < confirm_days:
        return "box_bottom_unconfirmed"
    if not 0 < bottom < top:
        return "flat_box"
    return Box(top, bottom, len(tops) - start)


def darvas_box(ctx: EvalContext, params: DarvasBoxParams) -> CriterionResult:
    """HAT-CRIT-124. Passes with `pivot` (the box top) and `final_contraction_low` (the box
    bottom) when the box is confirmed, at most `max_box_days` old and at most `max_depth_pct`
    deep; the value is the depth in percent. Without a full window of bars it is `no_data`."""
    bars = ctx.history
    highs = last(bars.high, params.lookback_days)
    lows = last(bars.low, params.lookback_days)
    if highs is None or lows is None:
        return no_data()
    box = find_box(highs, lows, params.confirm_days)
    if isinstance(box, str):
        return verdict(False, None, box)
    depth = float((box.top - box.bottom) * 100 / box.top)
    if box.age > params.max_box_days:
        return verdict(False, float(box.age), "box_too_old")
    if (box.top - box.bottom) * 100 > box.top * dec(params.max_depth_pct):
        return verdict(False, depth, "box_too_deep")
    levels = {"pivot": float(box.top), "final_contraction_low": float(box.bottom)}
    return CriterionResult(True, 1.0, depth, REASON_OK, levels)
