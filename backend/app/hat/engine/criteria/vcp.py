"""`vcp`: the scored volatility-contraction detector (HAT-CRIT-040), ported from the legacy
`services/vcp/pattern.py` (frozen outputs in `tests/hat/fixtures/vcp_oracle.json`).

What carries over unchanged is the structure search: the base starts at the first highest close
of the last `lookback_days` sessions (and must hold at least `MIN_BASE_BARS` bars); pivots are
bars strictly beyond their three neighbours on each side; contractions chain pivot-high to the
next pivot-low, then to the next pivot-high after it, up to the allowed maximum. What the spec
changes: the tightening test is the ratio rule (each depth at most `depth_ratio_max` x the
previous one, the final depth at most `final_depth_max_pct`), the dry-up test is the final
contraction's mean volume against the mean of the 50 sessions before the base starts
(`dryup_vs_avg50`, PR 57), a contraction's depth
is measured from the highest high to the lowest low of its own bars, and the result is a score
with the pivot and the final contraction low. The legacy stage-2, RS, overhead-resistance,
volume-slope, grade and breakout gates are other criteria's work (or dropped) and are not here.

PR 57 (research G25-G27) adds three structural rules, each off by default for backtest sweeps:
level highs (`max_high_spread_pct`), a minimum first depth (`min_first_depth_pct`) and a minimum
contraction length (`min_contraction_bars`); and `pivot_source: base_high`, under which the
pattern only scores: any base passes with the base's highest high as the pivot, so a
template-only variant still has a breakout level.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .base import (
    REASON_OK,
    CriterionResult,
    EvalContext,
    ParamModel,
    clamp01,
    dec,
    no_data,
    verdict,
)
from .indicators import dsum, last

PIVOT_WINDOW = 3
MIN_BASE_BARS = 15
AVG_VOLUME_DAYS = 50
CONTRACTION_CAP = 6


class VcpParams(ParamModel):
    contractions: list[int] = Field(default_factory=lambda: [2, 6], min_length=2, max_length=2)
    depth_ratio_max: float = Field(default=0.75, gt=0, le=1)
    final_depth_max_pct: float = Field(default=10.0, gt=0, le=100)
    lookback_days: int = Field(default=120, ge=MIN_BASE_BARS, le=252)
    dryup_vs_avg50: float = Field(default=0.7, gt=0, le=10)
    # PR 57, each off (None) by default: every contraction high within this % of the highest one,
    # the first contraction at least this deep, and every contraction at least this many bars.
    max_high_spread_pct: float | None = Field(default=None, gt=0, le=100)
    min_first_depth_pct: float | None = Field(default=None, gt=0, le=100)
    min_contraction_bars: int | None = Field(default=None, ge=2, le=60)
    # `base_high`: the pattern scores but does not gate, and the pivot is the base's highest high.
    pivot_source: Literal["vcp", "base_high"] = "vcp"

    @model_validator(mode="after")
    def _check(self) -> VcpParams:
        low, high = self.contractions
        if not 2 <= low <= high <= CONTRACTION_CAP:
            raise ValueError("contractions is [min, max] with 2 <= min <= max <= 6")
        return self


@dataclass(frozen=True)
class Contraction:
    """A pullback inside the base: bar indices into the base (`start` the pivot high, `end` the
    pivot low), the highest high and lowest low of bars `start..end`, and the depth in percent."""

    start: int
    end: int
    high: Decimal
    low: Decimal

    @property
    def depth_pct(self) -> Decimal:
        return (self.high - self.low) / self.high * 100


def base_start(close: Sequence[float | None], lookback: int) -> int | None:
    """Index (into `close`) of the first highest close of the last `lookback` bars, or None when
    a close is missing there."""
    window = last(close, min(lookback, len(close)))
    if not window:
        return None
    top = max(window)
    return len(close) - len(window) + window.index(top)


def pivot_points(
    high: Sequence[float], low: Sequence[float], window: int = PIVOT_WINDOW
) -> tuple[list[int], list[int]]:
    """(pivot-high indices, pivot-low indices): bars strictly beyond `window` neighbours on each
    side."""
    highs: list[int] = []
    lows: list[int] = []
    for i in range(window, len(high) - window):
        around = [*range(i - window, i), *range(i + 1, i + window + 1)]
        if all(high[i] > high[j] for j in around):
            highs.append(i)
        if all(low[i] < low[j] for j in around):
            lows.append(i)
    return highs, lows


def contraction_chain(
    high: Sequence[float], low: Sequence[float], limit: int
) -> list[tuple[int, int]]:
    """The (pivot-high, pivot-low) index pairs of the successive pullbacks, at most `limit`. The
    first pullback starts at the base's first bar."""
    pivot_highs, pivot_lows = pivot_points(high, low)
    chain: list[tuple[int, int]] = []
    start = 0
    while True:
        ends = [i for i in pivot_lows if i > start]
        if not ends:
            break
        chain.append((start, ends[0]))
        starts = [i for i in pivot_highs if i > ends[0]]
        if not starts or len(chain) >= limit:
            break
        start = starts[0]
    return chain


def build_contractions(
    high: Sequence[float], low: Sequence[float], chain: Sequence[tuple[int, int]]
) -> list[Contraction]:
    return [
        Contraction(a, b, dec(max(high[a : b + 1])), dec(min(low[a : b + 1]))) for a, b in chain
    ]


def vcp_score(
    count: int, max_ratio: float, final_depth: float, dryup: float, params: VcpParams
) -> float:
    """The mean of four sub-scores, each 1 at the tightest value and 0 at its parameter limit:
    whether the count is in range, the worst depth ratio, the final depth and the dry-up."""
    low, high = params.contractions
    in_range = 1.0 if low <= count <= high else 0.0
    ratio = clamp01(1.0 - max_ratio / params.depth_ratio_max)
    depth = clamp01(1.0 - final_depth / params.final_depth_max_pct)
    quiet = clamp01(1.0 - dryup / params.dryup_vs_avg50)
    return (in_range + ratio + depth + quiet) / 4.0


def _pattern_fault(
    contractions: Sequence[Contraction], ratios: Sequence[Decimal], params: VcpParams
) -> tuple[str, float] | None:
    """The first structural rule the contractions (with their consecutive depth `ratios`) break,
    as (reason, value), or None."""
    lo_count, _ = params.contractions
    if len(contractions) < lo_count:
        return "too_few_contractions", float(len(contractions))
    if len(ratios) != len(contractions) - 1 or any(r > dec(params.depth_ratio_max) for r in ratios):
        return "depth_ratio_too_wide", float(len(contractions))
    first, final = contractions[0], contractions[-1]
    if final.depth_pct > dec(params.final_depth_max_pct):
        return "final_depth_too_deep", float(final.depth_pct)
    if params.min_contraction_bars is not None:
        shortest = min(c.end - c.start + 1 for c in contractions)
        if shortest < params.min_contraction_bars:
            return "contraction_too_short", float(shortest)
    if params.min_first_depth_pct is not None and first.depth_pct < dec(params.min_first_depth_pct):
        return "first_depth_too_shallow", float(first.depth_pct)
    if params.max_high_spread_pct is not None:
        top = max(c.high for c in contractions)
        spread = (top - min(c.high for c in contractions)) / top * 100
        if spread > dec(params.max_high_spread_pct):
            return "highs_not_level", float(spread)
    return None


def vcp(ctx: EvalContext, params: VcpParams) -> CriterionResult:
    """HAT-CRIT-040. Passes with the score, the contraction list, `pivot` (the highest high of
    the final contraction) and `final_contraction_low` (its lowest low), or fails with the first
    rule broken as the reason. A base is the bars from the first highest close of the lookback
    to the date, at least `MIN_BASE_BARS` long (`base_too_short`) with the 50 sessions before it
    (`no_data` without them), which are the dry-up baseline. With `pivot_source: base_high` every
    base passes: the score is the pattern's when it holds and 0 when not (`pattern` names the
    rule broken), `pivot` is the base's highest high, and `final_contraction_low` the last
    contraction's low (the base's lowest low without one); a base whose low equals its high
    fails `flat_base`, so the low is always below the pivot."""
    bars = ctx.history
    start = base_start(bars.close, params.lookback_days)
    if start is None:
        return no_data()
    baseline = last(bars.volume, AVG_VOLUME_DAYS, offset=len(bars) - start)
    if baseline is None:
        return no_data()
    high, low, volume = bars.high[start:], bars.low[start:], bars.volume[start:]
    if len(high) < MIN_BASE_BARS:
        return verdict(False, None, "base_too_short")
    if any(v is None for v in (*high, *low, *volume)):
        return no_data()
    highs = [float(v) for v in high if v is not None]
    lows = [float(v) for v in low if v is not None]
    volumes = [float(v) for v in volume if v is not None]
    _, hi_count = params.contractions
    contractions = build_contractions(highs, lows, contraction_chain(highs, lows, hi_count))
    depths = [c.depth_pct for c in contractions]
    ratios = [b / a for a, b in zip(depths, depths[1:], strict=False) if a > 0]
    fault = _pattern_fault(contractions, ratios, params)
    dryup: Decimal | None = None
    if fault is None:
        mean_avg = dsum(baseline) / AVG_VOLUME_DAYS
        if mean_avg <= 0:
            return no_data()
        final = contractions[-1]
        mean_final = dsum(volumes[final.start : final.end + 1]) / (final.end - final.start + 1)
        dryup = mean_final / mean_avg
        if dryup > dec(params.dryup_vs_avg50):
            fault = ("no_volume_dryup", float(dryup))
    if fault is not None and params.pivot_source == "vcp":
        return verdict(False, fault[1], fault[0])
    score = 0.0
    if dryup is not None and fault is None:
        score = vcp_score(
            len(contractions),
            float(max(ratios, default=Decimal(0))),
            float(depths[-1]),
            float(dryup),
            params,
        )
    if params.pivot_source == "base_high":
        pivot = dec(max(highs))
        floor = contractions[-1].low if contractions else dec(min(lows))
        if floor >= pivot:
            return verdict(False, float(pivot), "flat_base")
    else:
        pivot, floor = contractions[-1].high, contractions[-1].low
    base_dates = bars.dates[start:]
    extras: dict[str, object] = {
        "pivot": float(pivot),
        "final_contraction_low": float(floor),
        "contraction_count": len(contractions),
        "contractions": [
            {
                "start": base_dates[c.start].isoformat(),
                "end": base_dates[c.end].isoformat(),
                "depth_pct": float(c.depth_pct),
            }
            for c in contractions
        ],
        "base_start": base_dates[0].isoformat(),
    }
    if dryup is not None:
        extras["dryup_ratio"] = float(dryup)
    if params.pivot_source == "base_high":
        extras["pattern"] = "ok" if fault is None else fault[0]
    return CriterionResult(True, score, float(len(contractions)), REASON_OK, extras)
