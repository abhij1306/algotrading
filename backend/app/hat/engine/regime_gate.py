"""The strategy regime gate: one binary rule per bot over an index close series (HAT-BOT-053,
HAT-RISK-050, stage 2).

Pure and exact: closes are Decimals, the SMA comparisons are made on sums (`indicators.sma_sum`,
no division), and a series too short for the rule's lookback gives False (fail closed: no entries
without evidence). Each rule of `config.RegimeRule` has its function in `RULES`. The portfolio
regime (05) is separate and only scales weights; nothing here reads it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from .criteria.indicators import sma_sum
from .strategy.config import CloseAboveSma, FastAboveSlow, Regime

__all__ = [
    "RULES",
    "close_above_sma_and_rising",
    "regime_allows_longs",
    "sma_fast_above_slow_both_rising",
]


def close_above_sma_and_rising(closes: Sequence[Decimal], rule: CloseAboveSma) -> bool:
    """close_t > SMA_t and SMA_t > SMA_{t - rising_days} (the Minervini-style rule); with
    `rising_days` 0 the close above the SMA alone (no slope test)."""
    now = sma_sum(closes, rule.sma)
    if now is None:
        return False
    if rule.rising_days == 0:
        return closes[-1] * rule.sma > now
    before = sma_sum(closes, rule.sma, offset=rule.rising_days)
    if before is None:
        return False
    # close > now/sma and now > before, compared without dividing
    return closes[-1] * rule.sma > now and now > before


def sma_fast_above_slow_both_rising(closes: Sequence[Decimal], rule: FastAboveSlow) -> bool:
    """SMA_fast_t > SMA_slow_t, and both are higher than `rising_days` sessions earlier (the
    Qullamaggie-style rule; the config's default is 3, `FastAboveSlow`)."""
    fast, slow, back = rule.fast, rule.slow, rule.rising_days
    fast_now, slow_now = sma_sum(closes, fast), sma_sum(closes, slow)
    fast_before = sma_sum(closes, fast, offset=back)
    slow_before = sma_sum(closes, slow, offset=back)
    if fast_now is None or slow_now is None or fast_before is None or slow_before is None:
        return False
    # SMA_fast > SMA_slow, compared as sums cross-multiplied by the other window
    return fast_now * slow > slow_now * fast and fast_now > fast_before and slow_now > slow_before


RULES: dict[type, Callable[[Sequence[Decimal], Any], bool]] = {
    CloseAboveSma: close_above_sma_and_rising,
    FastAboveSlow: sma_fast_above_slow_both_rising,
}


def regime_allows_longs(regime: Regime, closes: Sequence[Decimal]) -> bool:
    """Whether the bot's configured rule lets it open new longs on the last close in `closes`."""
    rule = regime.parsed()
    return RULES[type(rule)](closes, rule)
