"""`ema_pullback`: a pullback to a rising short EMA on lighter volume (HAT-CRIT-125).

The trend-pullback entry (Minervini, Trade Like a Stock Market Wizard, 2013, on buying a leader's
low-volume pullback to its 10- or 20-day line; O'Neil, How to Make Money in Stocks, on the
"pullback to the 10-week line" as a later buy point): a leader that ran up comes back, on
volume below its average, to a rising 10- or 20-session EMA and holds it. The next session's
break of the scan day's high is the reclaim that enters; the stop goes just under the lowest low
of the pullback.

The criterion exposes the scan day's high as `pivot` and the pullback low as
`final_contraction_low`, the levels the trigger (`price_breaks_level: pivot`) and the stop rule
(`below_final_contraction_low`) read.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from pydantic import Field, model_validator

from .base import REASON_OK, CriterionResult, EvalContext, ParamModel, dec, no_data, verdict
from .indicators import dsum, last

# An EMA is seeded with the SMA of the first `length` closes of the last `length` x this many
# closes, so its value never depends on how much history the caller loaded; the seed's weight in
# the result is then below 0.1% for any length.
EMA_WARMUP = 6
EMA_MAX = 60


class EmaPullbackParams(ParamModel):
    """`emas`: the EMA lengths, the pullback may hold any one of them. A pullback runs from the
    highest high of the last `max_days` sessions to the date, at least `min_days` sessions after
    it. The date's low comes within `touch_pct` percent above the EMA and its close is at most
    `max_undercut_pct` percent below it; the EMA is above its value `rising_days` sessions
    earlier; the pullback's mean volume is below `volume_vs_avg50` x the 50-session mean."""

    emas: list[int] = Field(default_factory=lambda: [10, 20], min_length=1, max_length=3)
    rising_days: int = Field(default=5, ge=1, le=60)
    touch_pct: float = Field(default=1.0, ge=0, le=20)
    max_undercut_pct: float = Field(default=2.0, ge=0, le=20)
    min_days: int = Field(default=2, ge=1, le=60)
    max_days: int = Field(default=10, ge=2, le=60)
    volume_vs_avg50: float = Field(default=1.0, gt=0, le=10)

    @model_validator(mode="after")
    def _check(self) -> EmaPullbackParams:
        if len(set(self.emas)) != len(self.emas) or any(not 2 <= e <= EMA_MAX for e in self.emas):
            raise ValueError("emas are distinct EMA lengths of 2 to 60 sessions")
        if self.min_days >= self.max_days:
            raise ValueError("min_days must be below max_days")
        return self


def ema(closes: Sequence[float | None], length: int, *, offset: int = 0) -> Decimal | None:
    """The `length`-session EMA (multiplier 2 / (length + 1)) of the closes ending `offset` rows
    before the last, over the `EMA_WARMUP` x `length` closes ending there and seeded with the SMA
    of the first `length` of them; None unless that window exists and holds no null."""
    window = last(closes, length * EMA_WARMUP, offset=offset)
    if window is None:
        return None
    alpha = Decimal(2) / (length + 1)
    value = dsum(window[:length]) / length
    for close in window[length:]:
        value += alpha * (dec(close) - value)
    return value


def pullback_span(highs: Sequence[float], min_days: int) -> int | str:
    """The sessions since the window's highest high (its last session on a tie), or the reason
    there is no pullback: the high is under `min_days` sessions old."""
    top = max(highs)
    start = len(highs) - 1 - list(reversed(highs)).index(top)
    span = len(highs) - 1 - start
    return span if span >= min_days else "no_pullback"


def ema_fails(
    closes: Sequence[float | None],
    length: int,
    low: Decimal,
    close: Decimal,
    params: EmaPullbackParams,
) -> str | None:
    """Why the `length` EMA is not held (`no_data`, `ema_not_rising`, `above_ema`: the date's
    low did not come within `touch_pct`, `below_ema`: the close undercut it too far), or None."""
    now = ema(closes, length)
    before = ema(closes, length, offset=params.rising_days)
    if now is None or before is None:
        return "no_data"
    if now <= before:
        return "ema_not_rising"
    if low * 100 > now * (100 + dec(params.touch_pct)):
        return "above_ema"
    if close * 100 < now * (100 - dec(params.max_undercut_pct)):
        return "below_ema"
    return None


def held_ema(
    closes: Sequence[float | None], low: Decimal, close: Decimal, params: EmaPullbackParams
) -> int | str:
    """The first EMA of `emas` that holds, or the first reason with data why none does."""
    reasons = []
    for length in params.emas:
        reason = ema_fails(closes, length, low, close, params)
        if reason is None:
            return length
        reasons.append(reason)
    return next((r for r in reasons if r != "no_data"), "no_data")


def ema_pullback(ctx: EvalContext, params: EmaPullbackParams) -> CriterionResult:
    """HAT-CRIT-125. Passes with `pivot` (the date's high) and `final_contraction_low` (the
    pullback's lowest low) when a pullback of `min_days` to `max_days` sessions, on lighter
    volume, holds a rising EMA; the value is the EMA length held (on a volume failure, the
    pullback's volume over the 50-session mean). Without the bars it needs it is `no_data`."""
    bars = ctx.history
    highs, lows = last(bars.high, params.max_days + 1), last(bars.low, params.max_days + 1)
    volumes, closes = last(bars.volume, 50), last(bars.close, 1)
    if highs is None or lows is None or volumes is None or closes is None:
        return no_data()
    span = pullback_span(highs, params.min_days)
    if isinstance(span, str):
        return verdict(False, None, span)
    pulled, average = dsum(volumes[-span:]) * 50, dsum(volumes) * span
    if average <= 0:
        return no_data()
    if pulled >= average * dec(params.volume_vs_avg50):
        return verdict(False, float(pulled / average), "volume_not_lighter")
    held = held_ema(bars.close, dec(lows[-1]), dec(closes[-1]), params)
    if isinstance(held, str):
        return no_data() if held == "no_data" else verdict(False, None, held)
    levels = {"pivot": highs[-1], "final_contraction_low": min(lows[-span:])}
    return CriterionResult(True, 1.0, float(held), REASON_OK, levels)
