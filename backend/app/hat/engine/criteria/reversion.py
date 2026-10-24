"""`rsi_below`: a short-term oversold close by Wilder's RSI (HAT-CRIT-126).

The mean-reversion setup (L. Connors and C. Alvarez, Short Term Trading Strategies That Work,
2008, on the 2-period RSI): a stock in a long-term uptrend whose 2-session RSI closes below 10
(5 the stricter reading) has sold off hard enough to snap back within days. The trend filter (the
close above its 200-session SMA) is the library's `sma_above`.

The criterion exposes the scan date's close as `pivot`: the next session enters on a trade back
above it (`price_breaks_level: pivot`), the nearest the live trigger comes to Connors' entry at
the signal close.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from pydantic import Field

from .base import REASON_OK, CriterionResult, EvalContext, ParamModel, dec, no_data, verdict
from .indicators import last

# Wilder's averages are seeded with the simple mean of the first `length` changes of the last
# `length` x this many changes, so the RSI never depends on how much history the caller loaded;
# the seed's weight in the result, (1 - 1/length) ** (9 x length), is then below 0.02%.
RSI_WARMUP = 10
RSI_MAX = 50


class RsiBelowParams(ParamModel):
    """`length`: the RSI's sessions; `below`: the RSI must be strictly under this value."""

    length: int = Field(default=2, ge=2, le=RSI_MAX)
    below: float = Field(default=10.0, gt=0, le=100)


def wilder_rsi(closes: Sequence[float | None], length: int) -> Decimal | None:
    """Wilder's `length`-session RSI at the last close, over the `RSI_WARMUP` x `length` changes
    ending there: 100 - 100 / (1 + average gain / average loss), 100 with no loss; None unless
    that window exists and holds no null, or it holds no change at all."""
    window = last(closes, length * RSI_WARMUP + 1)
    if window is None:
        return None
    changes = [dec(b) - dec(a) for a, b in zip(window, window[1:], strict=False)]
    gain = sum((max(c, Decimal(0)) for c in changes[:length]), Decimal(0)) / length
    loss = sum((max(-c, Decimal(0)) for c in changes[:length]), Decimal(0)) / length
    for change in changes[length:]:
        gain = (gain * (length - 1) + max(change, Decimal(0))) / length
        loss = (loss * (length - 1) + max(-change, Decimal(0))) / length
    if loss == 0:
        return None if gain == 0 else Decimal(100)
    return 100 - 100 / (1 + gain / loss)


def rsi_below(ctx: EvalContext, params: RsiBelowParams) -> CriterionResult:
    """HAT-CRIT-126. Passes with `pivot` (the date's close) when the RSI is under `below`; the
    value is the RSI. Without the closes it needs it is `no_data`."""
    closes = ctx.history.close
    rsi = wilder_rsi(closes, params.length)
    if rsi is None:
        return no_data()
    if rsi >= dec(params.below):
        return verdict(False, float(rsi), "rsi_not_below")
    return CriterionResult(True, 1.0, float(rsi), REASON_OK, {"pivot": closes[-1]})
