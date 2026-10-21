"""Setup criteria (04 §4, HAT-CRIT-105..114): trend, relative strength and base quality."""

from __future__ import annotations

import math
from decimal import Decimal
from fractions import Fraction
from typing import Literal

import polars as pl
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
from .indicators import last, mean_gt, sma_sum

SMA_MAX = 400
LEVEL = Literal["close"]


class SmaAboveParams(ParamModel):
    """`a` and `b` are `"close"` or an SMA length in sessions."""

    a: int | LEVEL
    b: int | LEVEL

    @model_validator(mode="after")
    def _check(self) -> SmaAboveParams:
        for side in (self.a, self.b):
            if side != "close" and not 2 <= int(side) <= SMA_MAX:
                raise ValueError("an SMA length is 2 to 400 sessions")
        if self.a == self.b:
            raise ValueError("a and b must differ")
        return self


def _level(close: list[float | None], side: int | str) -> tuple[Decimal, int] | None:
    """(sum, count) whose ratio is the close or the SMA of that length."""
    if side == "close":
        return None if not close or close[-1] is None else (dec(close[-1]), 1)
    total = sma_sum(close, int(side))
    return None if total is None else (total, int(side))


def sma_above(ctx: EvalContext, params: SmaAboveParams) -> CriterionResult:
    """HAT-CRIT-105: value(a) > value(b), strictly; null when either side lacks bars."""
    close = ctx.history.close
    left, right = _level(close, params.a), _level(close, params.b)
    if left is None or right is None:
        return no_data()
    diff = float(left[0] / left[1] - right[0] / right[1])
    return verdict(mean_gt(left[0], left[1], right[0], right[1]), diff, "not_above")


class SmaRisingParams(ParamModel):
    sma: int = Field(default=200, ge=2, le=SMA_MAX)
    lookback_days: int = Field(default=21, ge=1, le=252)


def sma_rising(ctx: EvalContext, params: SmaRisingParams) -> CriterionResult:
    """HAT-CRIT-106: SMA(t) is higher than it was `lookback_days` sessions earlier."""
    close = ctx.history.close
    now = sma_sum(close, params.sma)
    before = sma_sum(close, params.sma, offset=params.lookback_days)
    if now is None or before is None:
        return no_data()
    return verdict(mean_gt(now, 1, before, 1), float((now - before) / params.sma), "not_rising")


class PctFrom52wLowParams(ParamModel):
    min_pct: float = Field(default=30.0, ge=0, le=10_000)


WINDOW_52W = 252


def pct_from_52w_low(ctx: EvalContext, params: PctFrom52wLowParams) -> CriterionResult:
    """HAT-CRIT-107: (close / low_52w - 1) x 100 >= min_pct."""
    bars = ctx.history
    lows = last(bars.low, WINDOW_52W)
    if lows is None or bars.close[-1] is None:
        return no_data()
    low, close = dec(min(lows)), dec(bars.close[-1])
    if low <= 0:
        return no_data()
    pct = float((close / low - 1) * 100)
    return verdict(close * 100 >= low * (100 + dec(params.min_pct)), pct, "too_close_to_low")


class PctFrom52wHighParams(ParamModel):
    max_pct_below: float = Field(default=25.0, ge=0, le=100)


def pct_from_52w_high(ctx: EvalContext, params: PctFrom52wHighParams) -> CriterionResult:
    """HAT-CRIT-108: (1 - close / high_52w) x 100 <= max_pct_below."""
    bars = ctx.history
    highs = last(bars.high, WINDOW_52W)
    if highs is None or bars.close[-1] is None:
        return no_data()
    high, close = dec(max(highs)), dec(bars.close[-1])
    if high <= 0:
        return no_data()
    pct = float((1 - close / high) * 100)
    return verdict(
        close * 100 >= high * (100 - dec(params.max_pct_below)), pct, "too_far_below_high"
    )


class RsRankParams(ParamModel):
    """`min_percentile` is the hard rule; `score_from` and `score_full` the score curve."""

    min_percentile: float = Field(default=70.0, ge=0, le=100)
    score_from: float = Field(default=70.0, ge=0, le=100)
    score_full: float = Field(default=95.0, ge=0, le=100)

    @model_validator(mode="after")
    def _check(self) -> RsRankParams:
        if self.score_from >= self.score_full:
            raise ValueError("score_from must be below score_full")
        return self


def _universe_value(ctx: EvalContext, column: str) -> float | None:
    frame = ctx.universe
    if frame is None or column not in frame.columns:
        return None
    row = frame.filter(pl.col("instrument_id") == ctx.instrument_id)
    if row.height != 1:
        return None
    value = row[column][0]
    return None if value is None or not math.isfinite(value) else float(value)


def rs_score(rs_pct: float, score_from: float, score_full: float) -> float:
    """0 at or below `score_from`, 1 at or above `score_full`, linear between."""
    if rs_pct <= score_from:
        return 0.0
    if rs_pct >= score_full:
        return 1.0
    return clamp01((rs_pct - score_from) / (score_full - score_from))


def rs_rank(ctx: EvalContext, params: RsRankParams) -> CriterionResult:
    """HAT-CRIT-109: `rs_pct` (02 §7) at least `min_percentile`; score from the linear curve."""
    rs = _universe_value(ctx, "rs_pct")
    if rs is None:
        return no_data()
    score = rs_score(rs, params.score_from, params.score_full)
    return verdict(rs >= params.min_percentile, rs, "rs_below_min", score=score)


MOM_WINDOWS = (21, 63, 126, 252)


class MomentumRankParams(ParamModel):
    windows: list[Literal[21, 63, 126, 252]] = Field(
        default_factory=lambda: [21, 63, 126], min_length=1, max_length=4
    )
    top_pct: float = Field(default=2.0, gt=0, le=100)
    match: Literal["any", "all"] = "any"

    @model_validator(mode="after")
    def _check(self) -> MomentumRankParams:
        if len(set(self.windows)) != len(self.windows):
            raise ValueError("windows repeat")
        return self


def in_top(universe: pl.DataFrame, column: str, instrument_id: str, top_pct: float) -> bool | None:
    """Whether the instrument's return is in the top `top_pct`% of the universe rows that have a
    value: fewer than ceil(top_pct / 100 x N) rows are strictly above it. None without a value."""
    if column not in universe.columns:
        return None
    values = universe.filter(pl.col(column).is_not_null() & pl.col(column).is_finite())
    mine = values.filter(pl.col("instrument_id") == instrument_id)
    if mine.height != 1:
        return None
    seats = math.ceil(Fraction(repr(top_pct)) * values.height / 100)
    above = values.filter(pl.col(column) > mine[column][0]).height
    return above < seats


def momentum_rank(ctx: EvalContext, params: MomentumRankParams) -> CriterionResult:
    """HAT-CRIT-110: top `top_pct`% of the universe on any (or every) return window."""
    if ctx.universe is None:
        return no_data()
    verdicts = [
        in_top(ctx.universe, f"ret{w}", ctx.instrument_id, params.top_pct) for w in params.windows
    ]
    if all(v is None for v in verdicts):
        return no_data()
    hits = sum(1 for v in verdicts if v)
    ok = hits == len(verdicts) if params.match == "all" else hits > 0
    return verdict(ok, float(hits), "not_top_momentum", score=hits / len(verdicts) if ok else 0.0)


class ConsolidationParams(ParamModel):
    days: list[int] = Field(default_factory=lambda: [5, 15], min_length=2, max_length=2)
    max_range_pct: float = Field(default=15.0, gt=0, le=1000)
    higher_lows: bool = True

    @model_validator(mode="after")
    def _check(self) -> ConsolidationParams:
        low, high = self.days
        if not 2 <= low <= high <= 60:
            raise ValueError("days is [min, max] with 2 <= min <= max <= 60")
        return self


def _window_range(
    high: list[float | None], low: list[float | None], length: int, params: ConsolidationParams
) -> tuple[Decimal, Decimal] | None:
    """(range %, window high) of the last `length` sessions when they qualify, else None."""
    highs, lows = last(high, length), last(low, length)
    if highs is None or lows is None:
        return None
    top, bottom = dec(max(highs)), dec(min(lows))
    if bottom <= 0:
        return None
    if params.higher_lows and dec(min(lows[length // 2 :])) < dec(min(lows[: length // 2])):
        return None
    rng = (top / bottom - 1) * 100
    return (rng, top) if rng <= dec(params.max_range_pct) else None


def consolidation(ctx: EvalContext, params: ConsolidationParams) -> CriterionResult:
    """HAT-CRIT-111: the tightest window of `days` sessions ending on the date whose high/low
    range is at most `max_range_pct` and, with `higher_lows`, whose second half never undercuts
    the first half's low. The score is 1 - range / max_range; `consolidation_high` is the
    window's highest high (ties: the longer window)."""
    bars = ctx.history
    low_days, high_days = params.days
    if last(bars.high, low_days) is None or last(bars.low, low_days) is None:
        return no_data()
    best: tuple[Decimal, int, Decimal] | None = None  # (range %, -length, window high)
    for length in range(low_days, high_days + 1):
        found = _window_range(bars.high, bars.low, length, params)
        if found is not None and (best is None or (found[0], -length) < best[:2]):
            best = (found[0], -length, found[1])
    if best is None:
        return verdict(False, None, "range_too_wide")
    score = clamp01(1.0 - float(best[0]) / params.max_range_pct)
    return CriterionResult(
        True, score, float(best[0]), REASON_OK, {"consolidation_high": float(best[2])}
    )


class MaSurfParams(ParamModel):
    mas: list[int] = Field(default_factory=lambda: [10, 20], min_length=1, max_length=5)
    max_distance_pct: float = Field(default=5.0, gt=0, le=100)

    @model_validator(mode="after")
    def _check(self) -> MaSurfParams:
        if len(set(self.mas)) != len(self.mas) or any(not 2 <= m <= SMA_MAX for m in self.mas):
            raise ValueError("mas are distinct SMA lengths of 2 to 400 sessions")
        return self


def ma_surf(ctx: EvalContext, params: MaSurfParams) -> CriterionResult:
    """HAT-CRIT-112: the close is within `max_distance_pct` of every SMA in `mas`; the score is
    1 - (largest distance) / max_distance_pct, floored at 0."""
    close = ctx.history.close
    if not close or close[-1] is None:
        return no_data()
    last_close = dec(close[-1])
    worst = Decimal(0)
    for length in params.mas:
        total = sma_sum(close, length)
        if total is None or total <= 0:
            return no_data()
        distance = abs(last_close * length - total) * 100 / total
        worst = max(worst, distance)
    passed = worst <= dec(params.max_distance_pct)
    score = clamp01(1.0 - float(worst) / params.max_distance_pct)
    return verdict(passed, float(worst), "too_far_from_ma", score=score if passed else 0.0)


class PriorMoveParams(ParamModel):
    min_pct: float = Field(default=30.0, gt=0, le=100_000)
    within_days: list[int] = Field(default_factory=lambda: [21, 63], min_length=2, max_length=2)

    @model_validator(mode="after")
    def _check(self) -> PriorMoveParams:
        low, high = self.within_days
        if not 2 <= low <= high <= WINDOW_52W:
            raise ValueError("within_days is [min, max] with 2 <= min <= max <= 252")
        return self


def prior_move(ctx: EvalContext, params: PriorMoveParams) -> CriterionResult:
    """HAT-CRIT-113: some low-then-high pair of sessions i < j inside the trailing window rose at
    least `min_pct` (high_j / low_i - 1). Windows of d = min..max sessions are nested, so the
    largest one that the history fills holds every candidate pair; fewer than `min` bars: no
    data."""
    bars = ctx.history
    low_days, high_days = params.within_days
    n = min(high_days, len(bars))
    if n < low_days:
        return no_data()
    highs, lows = last(bars.high, n), last(bars.low, n)
    if highs is None or lows is None:
        return no_data()
    need = 100 + dec(params.min_pct)
    running_low: Decimal | None = None
    best = Decimal(0)
    hit = False
    for high, low in zip(highs, lows, strict=True):
        if running_low is not None and running_low > 0:
            best = max(best, (dec(high) / running_low - 1) * 100)
            hit = hit or dec(high) * 100 >= running_low * need
        running_low = dec(low) if running_low is None else min(running_low, dec(low))
    return verdict(hit, float(best), "no_prior_move")


class EarningsGrowthParams(ParamModel):
    eps_yoy: float = Field(default=20.0, ge=-1000, le=100_000)
    sales_yoy: float = Field(default=20.0, ge=-1000, le=100_000)
    accelerating: bool = True


def earnings_growth(ctx: EvalContext, params: EarningsGrowthParams) -> CriterionResult:
    """HAT-CRIT-114: registered, off in every MVP config, and without a data source until Phase 2.
    Only rows filed on or before the date count; without any the result is `no_data`. With rows:
    the latest row's EPS and sales growth meet the minimums and, when `accelerating`, its EPS
    growth beats the row before (docs silent; owner to confirm)."""
    frame = ctx.fundamentals
    if frame is None:
        return no_data()
    rows = frame.filter(pl.col("filed_at") <= ctx.as_of).sort("filed_at", maintain_order=True)
    if rows.is_empty():
        return no_data()
    latest = rows.row(-1, named=True)
    eps, sales = latest["eps_yoy"], latest["sales_yoy"]
    if eps is None or sales is None:
        return no_data()
    ok = eps >= params.eps_yoy and sales >= params.sales_yoy
    if ok and params.accelerating:
        if rows.height < 2 or rows["eps_yoy"][-2] is None:
            return no_data()
        ok = eps > rows["eps_yoy"][-2]
    return verdict(ok, float(eps), "growth_below_min")


class NeglectParams(ParamModel):
    max_return_63d_pct: float = Field(default=30.0, ge=-100, le=100_000)


def neglect(ctx: EvalContext, params: NeglectParams) -> CriterionResult:
    """HAT-CRIT-120: the 63-session return, on bars up to the previous session, is at most
    `max_return_63d_pct` (close_prev / close_63 sessions before - 1)."""
    close = ctx.previous_history.close
    window = last(close, 64)
    if window is None or window[0] <= 0:
        return no_data()
    last_close, base = dec(window[-1]), dec(window[0])
    pct = float((last_close / base - 1) * 100)
    return verdict(
        last_close * 100 <= base * (100 + dec(params.max_return_63d_pct)), pct, "already_extended"
    )
