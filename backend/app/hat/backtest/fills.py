"""The daily-bar fill proxies (HAT-BT-020, HAT-BT-021): pure and exact.

Daily bars cannot show where in the day a level broke, so the backtest assumes one documented
path through each day: **open, then low, then high, then close** (the adverse move first). An
entry trigger is a break of the setup's level on the way up; a stop is hit on the way down; a
close-based exit fills at the official close. The gap between this proxy and live fills is
measured in shadow mode.

The run records these conventions (`FILL_PATH`, the entry fill rule, `GAP_THROUGH_RULE`,
`TRIGGER_PROXY`) in its metrics and `config_digest` (PR 62). The default entry rule is
pessimistic: the level must be penetrated by one tick, as live needs a trade above it
(`price_breaks_level`, HAT-CRIT-119), and the fill is that first price above the level; `touch`
(a high at the level fills at the level) is kept as a variant for sensitivity runs.

These functions only choose a price. Whether an entry is taken (max chase, caps, sizing) is
`evaluate_signal`'s decision, and which exit fires is `exits.evaluate`'s; the backtest feeds
them the price chosen here.

What the open -> low -> high -> close path cannot settle (PR 64, HAT-BT-021): a stop raised on
the way up is placed after the low on the assumed path, but the day's low may as well have come
after the high. A day whose low reaches the stop the way up raised, and whose close stays above
it, is ambiguous (`raised_stop_dip`); the run counts such days, and under the `pessimistic` rule
(`--ambiguous pessimistic`) the raised stop fills at its price that day. The default,
`optimistic`, keeps the assumed path. A position whose instrument has no bar
for more than `SUSPENSION_SESSIONS` sessions (suspended, not delisted) is marked one NSE band
down per further session (`suspended_mark`).
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

__all__ = [
    "AMBIGUOUS_OPTIMISTIC",
    "AMBIGUOUS_PESSIMISTIC",
    "AMBIGUOUS_RULES",
    "ENTRY_FILL_RULES",
    "FILL_PATH",
    "GAP_THROUGH_RULE",
    "PENETRATION",
    "STOP_SLIPPAGE",
    "STOP_SLIPPAGE_MEASURED",
    "SUSPENSION_BAND_PCT",
    "SUSPENSION_SESSIONS",
    "TOUCH",
    "TRIGGER_PROXY",
    "entry_fill",
    "raised_stop_dip",
    "stop_fill",
    "suspended_mark",
]

FILL_PATH = "open_low_high_close"  # the assumed path through a daily bar
GAP_THROUGH_RULE = "min_open_stop"  # a stop gapped through fills at the open
TRIGGER_PROXY = "daily_bar_level_break"  # the trigger is the daily bar's break of the level
PENETRATION = "penetration_1_tick"  # the default entry rule: the high clears the level by a tick
TOUCH = "touch"  # the variant: a high at the level fills at the level
ENTRY_FILL_RULES = (PENETRATION, TOUCH)
AMBIGUOUS_OPTIMISTIC = "optimistic"  # the default: a raised stop holds unless the close is below
AMBIGUOUS_PESSIMISTIC = "pessimistic"  # a raised stop the day's low reached fills at its price
AMBIGUOUS_RULES = (AMBIGUOUS_OPTIMISTIC, AMBIGUOUS_PESSIMISTIC)
# The mean fill of a resting buy stop over its trigger on a session that did not open above the
# level, as a fraction of it: +0.12 pp on the 1,142 breaks of a 20-day high in the 5-minute bars
# of 2021-10-26..2021-12-31 (a break between 09:15 and 09:20 buys at the 09:20 price, +0.53 pp
# on average, 23% of the breaks). Fitted on 46 sessions only: provisional until refitted on the
# 5-minute years (`STOP_SLIPPAGE_MEASURED`; until then runs that use it report
# `stop_slippage_provisional`). `fillrules.stop_order_fill` applies it.
STOP_SLIPPAGE = Decimal("0.0012")
STOP_SLIPPAGE_MEASURED = False
SUSPENSION_SESSIONS = 5  # bar-less sessions a position is carried at its last close
SUSPENSION_BAND_PCT = Decimal(5)  # the markdown per further bar-less session
_HUNDRED = Decimal(100)


def entry_fill(
    open_: Decimal,
    high: Decimal,
    level: Decimal,
    tick: Decimal | None,
    rule: str = PENETRATION,
) -> Decimal | None:
    """The entry price of a break of `level` on a day (HAT-BT-020): the open when the day opens
    above the level (a gap over); otherwise, under `penetration_1_tick`, the first tick-grid
    price above the level (level + tick for a level on the grid) when the high reaches it, as
    live's first trade above the level, and under `touch` the level when the
    high reaches the level; None when the high falls short, or under `penetration_1_tick`
    without a positive tick."""
    if open_ > level:
        return open_
    if rule == TOUCH:
        return level if high >= level else None
    if rule != PENETRATION:
        raise ValueError(f"unknown entry fill rule: {rule}")
    if tick is None or tick <= 0:
        return None
    # The first price on the tick grid above the level: level + tick for a level on the grid; an
    # adjusted level between grid prices is cleared by the next one.
    above = (level / tick).to_integral_value(rounding=ROUND_FLOOR) * tick + tick
    return above if high >= above else None


def stop_fill(open_: Decimal, low: Decimal, stop: Decimal) -> Decimal | None:
    """The fill of a stop on a held day: min(open, stop) when the low reaches the stop (a gap
    through it fills at the open, never better than the stop), None otherwise."""
    if low <= stop or open_ <= stop:
        return min(open_, stop)
    return None


def raised_stop_dip(low: Decimal, close: Decimal, before: Decimal, after: Decimal) -> bool:
    """Whether a day is ambiguous (HAT-BT-021, PR 64): the way up raised the stop from `before`
    to `after`, the day's low reaches the raised stop and the close is above it. Whether that
    low came before the high (the stop held) or after it (the stop filled) is not in a daily
    bar; a close at or below the raised stop exits at it on either order."""
    return before < after and low <= after < close


def suspended_mark(
    last_close: Decimal, sessions: int, after: int, band_pct: Decimal, tick: Decimal
) -> Decimal:
    """The mark of a position whose instrument has had no bar for `sessions` sessions (PR 64):
    its last close for the first `after` of them, then one band of `band_pct` lower per further
    session, floored to the tick and never below one tick, nor above the last close (a last
    close under one tick stays there)."""
    excess = sessions - after
    if excess <= 0:
        return last_close
    raw = last_close * ((_HUNDRED - band_pct) / _HUNDRED) ** excess
    floored = (raw / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    return min(last_close, max(floored, tick))
