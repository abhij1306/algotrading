"""Universe criteria (HAT-CRIT-100..104): which instruments may be considered at all."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field

from .base import (
    REASON_NO_DATA,
    CriterionResult,
    EvalContext,
    ParamModel,
    dec,
    no_data,
    verdict,
)
from .indicators import daily_range_pct, dsum, last, traded_values

CRORE = Decimal(10) ** 7


class SeriesInParams(ParamModel):
    series: list[str] = Field(default_factory=lambda: ["EQ", "BE"], min_length=1, max_length=20)


def series_in(ctx: EvalContext, params: SeriesInParams) -> CriterionResult:
    """HAT-CRIT-100: the instrument's series is in the list."""
    if ctx.series is None:
        return no_data()
    return verdict(ctx.series in params.series, None, "series_not_allowed")


class MinTurnoverParams(ParamModel):
    days: int = Field(default=20, ge=1, le=252)
    inr_cr: float = Field(default=10.0, gt=0, le=1e6)


def min_turnover(ctx: EvalContext, params: MinTurnoverParams) -> CriterionResult:
    """HAT-CRIT-101: the mean traded value of the last `days` sessions is at least `inr_cr` crore
    (traded value, or close x volume where it is null)."""
    bars = ctx.history
    window = last(traded_values(bars.traded_value, bars.close, bars.volume), params.days)
    if window is None:
        return no_data()
    total = dsum(window)
    mean_cr = float(total / params.days / CRORE)
    return verdict(total >= dec(params.inr_cr) * CRORE * params.days, mean_cr, "turnover_below_min")


class MinPriceParams(ParamModel):
    inr: float = Field(default=50.0, ge=0, le=1e7)


def min_price(ctx: EvalContext, params: MinPriceParams) -> CriterionResult:
    """HAT-CRIT-102: the latest close is at least `inr`."""
    close = ctx.history.close
    if not close or close[-1] is None:
        return no_data()
    return verdict(dec(close[-1]) >= dec(params.inr), close[-1], "price_below_min")


BandName = Literal[2, 5, 10, 20, "none"]
DEFAULT_BANDS: tuple[BandName, ...] = (2, 5, 10, 20, "none")


class PriceBandAllowedParams(ParamModel):
    allowed: list[BandName] = Field(default_factory=lambda: list(DEFAULT_BANDS), min_length=1)


def current_band(bands: list[tuple[date, float | None]], as_of: date) -> BandName | None:
    """The band of the latest row effective on or before `as_of`; None when there is no row."""
    rows = sorted(((d, b) for d, b in bands if d <= as_of), key=lambda row: row[0])
    if not rows:
        return None
    pct = rows[-1][1]
    return "none" if pct is None else int(pct)


def price_band_allowed(ctx: EvalContext, params: PriceBandAllowedParams) -> CriterionResult:
    """HAT-CRIT-103: the instrument's band as of the date is in the allowed set."""
    band = current_band(list(ctx.price_bands), ctx.as_of)
    if band is None:
        return no_data()
    return verdict(
        band in params.allowed, None if band == "none" else float(band), "band_not_allowed"
    )


class AdrMinParams(ParamModel):
    days: int = Field(default=20, ge=1, le=252)
    pct: float = Field(default=4.0, ge=0, le=1000)


def adr_min(ctx: EvalContext, params: AdrMinParams) -> CriterionResult:
    """HAT-CRIT-104: the mean of 100 x (high / low - 1) over the last `days` sessions is at
    least `pct` (`adr20` for the default 20 days)."""
    bars = ctx.history
    ranges = daily_range_pct(bars.high, bars.low)
    n = params.days
    if len(ranges) < n or any(r is None for r in ranges[-n:]):
        return no_data(REASON_NO_DATA)
    total = sum((r for r in ranges[-n:] if r is not None), Decimal(0))
    return verdict(total >= dec(params.pct) * n, float(total / n), "adr_below_min")
