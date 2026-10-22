"""The daily-bar fill proxies (09 §2, HAT-BT-020, HAT-BT-021): pure and exact.

Daily bars cannot show where in the day a level broke, so the backtest assumes one documented
path through each day: **open, then low, then high, then close** (the adverse move first). An
entry trigger is a break of the setup's level on the way up; a stop is hit on the way down; a
close-based exit fills at the official close. The gap between this proxy and live fills is
measured in shadow mode (PLAN Q4).

The run records these conventions (`FILL_PATH`, the entry fill rule, `GAP_THROUGH_RULE`,
`TRIGGER_PROXY`) in its metrics and `config_digest` (PR 62). The default entry rule is
pessimistic: the level must be penetrated by one tick, as live needs a trade above it
(`price_breaks_level`, HAT-CRIT-119), and the fill is that first price above the level; `touch`
(a high at the level fills at the level) is kept as a variant for sensitivity runs.

These functions only choose a price. Whether an entry is taken (max chase, caps, sizing) is
`evaluate_signal`'s decision, and which exit fires is `exits.evaluate`'s; the backtest feeds
them the price chosen here.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

__all__ = [
    "ENTRY_FILL_RULES",
    "FILL_PATH",
    "GAP_THROUGH_RULE",
    "PENETRATION",
    "TOUCH",
    "TRIGGER_PROXY",
    "entry_fill",
    "stop_fill",
]

FILL_PATH = "open_low_high_close"  # the assumed path through a daily bar
GAP_THROUGH_RULE = "min_open_stop"  # a stop gapped through fills at the open
TRIGGER_PROXY = "daily_bar_level_break"  # the trigger is the daily bar's break of the level
PENETRATION = "penetration_1_tick"  # the default entry rule: the high clears the level by a tick
TOUCH = "touch"  # the variant: a high at the level fills at the level
ENTRY_FILL_RULES = (PENETRATION, TOUCH)


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
