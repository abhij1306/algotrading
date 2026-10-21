"""The daily-bar fill proxies (09 §2, HAT-BT-020, HAT-BT-021): pure and exact.

Daily bars cannot show where in the day a level broke, so the backtest assumes one documented
path through each day: **open, then low, then high, then close** (the adverse move first). An
entry trigger is a break of the setup's level on the way up; a stop is hit on the way down; a
close-based exit fills at the official close. The gap between this proxy and live fills is
measured in shadow mode (PLAN Q4).

These functions only choose a price. Whether an entry is taken (max chase, caps, sizing) is
`evaluate_signal`'s decision, and which exit fires is `exits.evaluate`'s; the backtest feeds
them the price chosen here.
"""

from __future__ import annotations

from decimal import Decimal

__all__ = ["entry_fill", "stop_fill"]


def entry_fill(open_: Decimal, high: Decimal, level: Decimal) -> Decimal | None:
    """The entry price of a break of `level` on a day: the open when the day opens above the
    level (a gap over), the level when the day's range reaches it from below, None when the high
    stays under it."""
    if open_ > level:
        return open_
    if high >= level:
        return level
    return None


def stop_fill(open_: Decimal, low: Decimal, stop: Decimal) -> Decimal | None:
    """The fill of a stop on a held day: min(open, stop) when the low reaches the stop (a gap
    through it fills at the open, never better than the stop), None otherwise."""
    if low <= stop or open_ <= stop:
        return min(open_, stop)
    return None
