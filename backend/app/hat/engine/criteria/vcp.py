"""`vcp`: the scored volatility-contraction detector (HAT-CRIT-040), ported from the legacy
`services/vcp/pattern.py` (frozen outputs in `tests/hat/fixtures/vcp_oracle.json`).

What carries over unchanged is the structure search: the base starts at the first highest close
of the last `lookback_days` sessions (and must hold at least `MIN_BASE_BARS` bars); pivots are
bars strictly beyond their three neighbours on each side; contractions chain pivot-high to the
next pivot-low, then to the next pivot-high after it, up to the allowed maximum. What the spec
changes: the tightening test is the ratio rule (each depth at most `depth_ratio_max` x the
previous one, the final depth at most `final_depth_max_pct`), the dry-up test is the final
contraction's mean volume against the 50-session mean (`dryup_vs_avg50`), a contraction's depth
is measured from the highest high to the lowest low of its own bars, and the result is a score
with the pivot and the final contraction low. The legacy stage-2, RS, overhead-resistance,
volume-slope, grade and breakout gates are other criteria's work (or dropped) and are not here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

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


def vcp(ctx: EvalContext, params: VcpParams) -> CriterionResult:
    """HAT-CRIT-040. Passes with the score, the contraction list, `pivot` (the highest high of
    the final contraction) and `final_contraction_low` (its lowest low), or fails with the first
    rule broken as the reason."""
    bars = ctx.history
    if len(bars) < AVG_VOLUME_DAYS:
        return no_data()
    start = base_start(bars.close, params.lookback_days)
    avg_volume = last(bars.volume, AVG_VOLUME_DAYS)
    if start is None or avg_volume is None:
        return no_data()
    high, low, volume = bars.high[start:], bars.low[start:], bars.volume[start:]
    if len(high) < MIN_BASE_BARS:
        return verdict(False, None, "base_too_short")
    if any(v is None for v in (*high, *low, *volume)):
        return no_data()
    highs = [float(v) for v in high if v is not None]
    lows = [float(v) for v in low if v is not None]
    volumes = [float(v) for v in volume if v is not None]
    lo_count, hi_count = params.contractions
    contractions = build_contractions(highs, lows, contraction_chain(highs, lows, hi_count))
    if len(contractions) < lo_count:
        return verdict(False, float(len(contractions)), "too_few_contractions")
    depths = [c.depth_pct for c in contractions]
    ratios = [b / a for a, b in zip(depths, depths[1:], strict=False) if a > 0]
    if len(ratios) != len(depths) - 1 or any(r > dec(params.depth_ratio_max) for r in ratios):
        return verdict(False, float(len(contractions)), "depth_ratio_too_wide")
    final = contractions[-1]
    if final.depth_pct > dec(params.final_depth_max_pct):
        return verdict(False, float(final.depth_pct), "final_depth_too_deep")
    mean_final = dsum(volumes[final.start : final.end + 1]) / (final.end - final.start + 1)
    mean_avg = dsum(avg_volume) / AVG_VOLUME_DAYS
    if mean_avg <= 0:
        return no_data()
    dryup = mean_final / mean_avg
    if dryup > dec(params.dryup_vs_avg50):
        return verdict(False, float(dryup), "no_volume_dryup")
    score = vcp_score(
        len(contractions),
        float(max(ratios, default=Decimal(0))),
        float(final.depth_pct),
        float(dryup),
        params,
    )
    base_dates = bars.dates[start:]
    return CriterionResult(
        True,
        score,
        float(len(contractions)),
        REASON_OK,
        {
            "pivot": float(final.high),
            "final_contraction_low": float(final.low),
            "contraction_count": len(contractions),
            "contractions": [
                {
                    "start": base_dates[c.start].isoformat(),
                    "end": base_dates[c.end].isoformat(),
                    "depth_pct": float(c.depth_pct),
                }
                for c in contractions
            ],
            "dryup_ratio": float(dryup),
            "base_start": base_dates[0].isoformat(),
        },
    )
