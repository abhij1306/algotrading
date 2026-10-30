"""The strategy regime gate: one binary rule per bot over an index close series or the market
breadth series (HAT-BOT-053, HAT-RISK-050, stage 2).

Pure and exact: closes and breadth are Decimals, the SMA and mean comparisons are made on sums
(`indicators.sma_sum`, no division), and a series too short for the rule's lookback, or a breadth
session without a value, gives False (fail closed: no entries without evidence). Both series end
at the scan's session (the night before the trade date), so an entry on D reads data up to D-1's
close. Each rule of `config.RegimeRule` has its function in `RULES`. The portfolio
regime (05) is separate and only scales weights; nothing here reads it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from ..money import require_exact
from .criteria.indicators import sma_sum
from .strategy.config import AlwaysOn, BreadthAbove, CloseAboveSma, FastAboveSlow, Regime

__all__ = [
    "BREADTH_SESSIONS",
    "RULES",
    "breadth_above",
    "close_above_sma_and_rising",
    "needs_breadth",
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


# The breadth sessions the gate walks, newest last: its hysteresis state is a function of these
# alone, so live (`daily.market.breadth_series`) and the backtest (`MarketView.breadth_through`)
# agree whenever each passes at least this many.
BREADTH_SESSIONS = 250


def breadth_above(breadth: Sequence[Decimal | None], rule: BreadthAbove) -> bool:
    """Whether the breadth regime is on at the last session of `breadth` (one `breadth_pct` per
    stored session, oldest first, None where the session has none). Walked over the last
    `BREADTH_SESSIONS`, starting off: a session turns it on when the mean of the last
    `smooth_days` values is at least `min_pct`, and off when it is below `off_below_pct`
    (`min_pct` when unset); a session whose window is short or misses a value turns it off."""
    n = rule.smooth_days
    on_at = require_exact(rule.min_pct, "min_pct", floats=True) * n
    off_pct = rule.min_pct if rule.off_below_pct is None else rule.off_below_pct
    off_at = require_exact(off_pct, "off_below_pct", floats=True) * n
    series = breadth[-BREADTH_SESSIONS:]
    on = False
    for end in range(1, len(series) + 1):
        window = series[max(0, end - n) : end]
        if len(window) < n or any(v is None for v in window):
            on = False
            continue
        total = sum(window, Decimal(0))  # type: ignore[arg-type]
        on = total >= off_at if on else total >= on_at
    return on


RULES: dict[type, Callable[[Sequence[Decimal], Sequence[Decimal | None], Any], bool]] = {
    CloseAboveSma: lambda closes, _, rule: close_above_sma_and_rising(closes, rule),
    FastAboveSlow: lambda closes, _, rule: sma_fast_above_slow_both_rising(closes, rule),
    BreadthAbove: lambda _, breadth, rule: breadth_above(breadth, rule),
    AlwaysOn: lambda *_: True,
}


def needs_breadth(regime: Regime) -> bool:
    """Whether the regime's rule reads the breadth series (the backtest loads it only then)."""
    return isinstance(regime.parsed(), BreadthAbove)


def regime_allows_longs(
    regime: Regime, closes: Sequence[Decimal], breadth: Sequence[Decimal | None]
) -> bool:
    """Whether the bot's configured rule lets it open new longs on the last session of the
    series: `closes` of the regime index and `breadth` (`breadth_above`), both oldest first."""
    rule = regime.parsed()
    return RULES[type(rule)](closes, breadth, rule)
