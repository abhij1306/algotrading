"""The strategy regime gate: one binary rule per bot over an index close series (HAT-BOT-053,
HAT-RISK-050, 04 §1 stage 2).

Pure and exact: closes are Decimals, the SMA comparisons are made on sums (no division), and a
series too short for the rule's lookback gives False (fail closed: no entries without evidence).
The portfolio regime (05) is separate and only scales weights; nothing here reads it.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from .strategy.config import Regime

__all__ = [
    "close_above_sma_and_rising",
    "regime_allows_longs",
    "sma_fast_above_slow_both_rising",
]


def _sum(closes: Sequence[Decimal], window: int, back: int = 0) -> Decimal | None:
    """The sum of the `window` closes ending `back` sessions before the last; None when the
    series does not reach that far or a close is not positive and finite."""
    end = len(closes) - back
    if window < 1 or back < 0 or end < window:
        return None
    chosen = closes[end - window : end]
    if any(not c.is_finite() or c <= 0 for c in chosen):
        return None
    return sum(chosen, Decimal(0))


def close_above_sma_and_rising(closes: Sequence[Decimal], sma: int, rising_days: int) -> bool:
    """close_t > SMA_t and SMA_t > SMA_{t - rising_days} (the Minervini-style rule)."""
    now = _sum(closes, sma)
    before = _sum(closes, sma, rising_days)
    if now is None or before is None:
        return False
    # close > now/sma and now > before, compared without dividing
    return closes[-1] * sma > now and now > before


def sma_fast_above_slow_both_rising(
    closes: Sequence[Decimal], fast: int, slow: int, rising_days: int = 1
) -> bool:
    """SMA_fast_t > SMA_slow_t, and both are higher than `rising_days` sessions earlier (the
    Qullamaggie-style rule; one session by default, an [OWNER-PENDING] choice)."""
    fast_now, slow_now = _sum(closes, fast), _sum(closes, slow)
    fast_before, slow_before = _sum(closes, fast, rising_days), _sum(closes, slow, rising_days)
    if fast_now is None or slow_now is None or fast_before is None or slow_before is None:
        return False
    # SMA_fast > SMA_slow, compared as sums cross-multiplied by the other window
    return fast_now * slow > slow_now * fast and fast_now > fast_before and slow_now > slow_before


def regime_allows_longs(regime: Regime, closes: Sequence[Decimal]) -> bool:
    """Whether the bot's configured rule lets it open new longs on the last close in `closes`."""
    params = regime.params
    if regime.rule == "close_above_sma_and_rising":
        return close_above_sma_and_rising(closes, params["sma"], params["rising_days"])
    if regime.rule == "sma_fast_above_slow_both_rising":
        return sma_fast_above_slow_both_rising(
            closes, params["fast"], params["slow"], params["rising_days"]
        )
    return False  # an unknown rule never opens the gate
