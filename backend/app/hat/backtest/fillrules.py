"""The pure fill rules of the day loop that read a daily bar (HAT-BT-020, HAT-BT-021,
HAT-BT-025): the entry day's low before the break, the upper and lower circuit inferred from a
bar, and the first price on a day's way up at which the live exit rules act."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from ..engine import exits, trigger

__all__ = ["first_trigger", "lower_circuit_locked", "pre_break_low", "upper_circuit_locked"]

# The NSE price bands an upper circuit is inferred at when the run has no band table (PR 61).
NSE_BANDS = (Decimal(2), Decimal(5), Decimal(10), Decimal(20))


def _acts(rules: Sequence[Any], state: exits.PositionState, price: Decimal) -> bool:
    result = exits.evaluate(rules, state, exits.ExitView(price), "live")
    return bool(result.actions or result.new_stop is not None or result.fired)


def pre_break_low(
    bar_open: Decimal,
    bar_low: Decimal,
    level: Decimal,
    adr20: Decimal | None,
    intraday: Sequence[tuple[Decimal, Decimal]] = (),
) -> tuple[Decimal, Decimal | None]:
    """(the day's low before an entry at `level` broke, its low after the break or None) on an
    entry day (HAT-BT-020, PR 60). With the session's 5-minute bars: the lowest low of the bars
    up to and including the first one whose high reaches the level, as live reads it, and the
    lowest low of the bars after that one (or the day low, when the bars miss it). Without
    them: max(day low, min(open, level x (1 - adr20 / 100))), and the day low as the low after
    the break when it is below that; with no `adr20` the whole day's low comes before the
    break."""
    if intraday:
        lows = [low for _, low in intraday]
        at = next((i for i, (high, _) in enumerate(intraday) if high >= level), len(lows) - 1)
        # The trigger bar's low counts before the break (the stop is planned from it); a day low
        # the bars miss is taken as after it.
        later = [*lows[at + 1 :], *([bar_low] if bar_low < min(lows) else [])]
        return min(lows[: at + 1]), (min(later) if later else None)
    if adr20 is None or adr20 < 0:
        return bar_low, None
    estimate = min(bar_open, level * (1 - adr20 / 100))
    before = max(bar_low, estimate)
    return before, (bar_low if bar_low < before else None)


def upper_circuit_locked(
    price: Decimal, bar_high: Decimal, prev_close: Decimal | None, tick: Decimal | None
) -> bool | None:
    """Whether an entry at `price` is at the upper circuit (HAT-BT-025, PR 61), live's rule
    (`trigger.locked_upper_circuit`: the price at or above the band's upper price) read from a
    daily bar: the price is the day's high (nothing traded above it), above the previous close,
    and within one tick of an NSE band's upper price (`trigger.upper_circuit_price` at 2, 5, 10
    or 20%) over the previous close. The run has no band table, so the band is the nearest of
    these whose upper price is above the previous close; a name with a wider band entered at
    exactly a narrower one's price reads as locked. None without a previous close or a tick (live
    fails closed on an unknown circuit state)."""
    if prev_close is None or prev_close <= 0 or tick is None or tick <= 0:
        return None
    if price != bar_high or price <= prev_close:
        return False
    for band in NSE_BANDS:
        daily = trigger.DailyInputs(
            prev_close=prev_close, tick=tick, band_known=True, band_pct=band
        )
        upper = trigger.upper_circuit_price(daily)
        if upper is not None and upper > prev_close and abs(price - upper) <= tick:
            return True
    return False


def lower_circuit_locked(
    ohlc: tuple[Decimal, Decimal, Decimal, Decimal],
    prev_close: Decimal | None,
    tick: Decimal | None,
) -> bool | None:
    """Whether a session sat at its lower circuit all day (HAT-BT-021, PR 64), read from its
    daily bar as `upper_circuit_locked` reads the upper one: one price all day (open = high =
    low = close), below the previous close and within one tick of an NSE band's lower price
    (`trigger.lower_circuit_price` at 2, 5, 10 or 20%) under the previous close. Nothing traded
    above the band, so a seller found no buyer: a stop that day carries to the next session not
    pinned. None without a previous close or a tick."""
    if prev_close is None or prev_close <= 0 or tick is None or tick <= 0:
        return None
    open_, high, low, close = ohlc
    if not open_ == high == low == close or close >= prev_close:
        return False
    for band in NSE_BANDS:
        daily = trigger.DailyInputs(
            prev_close=prev_close, tick=tick, band_known=True, band_pct=band
        )
        lower = trigger.lower_circuit_price(daily)
        if lower is not None and lower < prev_close and abs(close - lower) <= tick:
            return True
    return False


def first_trigger(
    rules: Sequence[Any],
    state: exits.PositionState,
    low: Decimal,
    high: Decimal,
    tick: Decimal,
) -> Decimal | None:
    """The lowest price in [low, high] (`low`, `high` or a tick price between) at which the live
    rules of the stack act, or None when they do not act even at `high`.

    Above the stop every live rule that acts at a price also acts at any higher one (profit
    targets and R levels are thresholds), so the search is a bisection over the tick grid. The
    rules themselves are only ever evaluated by `exits.evaluate`."""
    if high < low or not _acts(rules, state, high):
        return None
    if _acts(rules, state, low):
        return low
    first = (low / tick).to_integral_value(rounding=ROUND_FLOOR) + 1
    last = (high / tick).to_integral_value(rounding=ROUND_CEILING) - 1
    lo, hi = int(first), int(last) + 1  # index hi stands for `high` itself
    while lo < hi:
        mid = (lo + hi) // 2
        if _acts(rules, state, mid * tick):
            hi = mid
        else:
            lo = mid + 1
    return high if lo > int(last) else lo * tick
