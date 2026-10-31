"""The pure fill rules of the day loop that read a daily bar (HAT-BT-020, HAT-BT-021,
HAT-BT-025): the entry day's low before the break, the upper and lower circuit inferred from a
bar, the first price on a day's way up at which the live exit rules act, the close entry's
breakout volume test, `volume_pace` on the full day's volume and the daily estimate of a stop
order's fill."""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from ..engine import exits, trigger
from ..engine.fill_0920 import gap_over_fill
from ..engine.stops import SKIP_BAD_INPUT
from ..money import require_exact
from .fills import STOP_SLIPPAGE

__all__ = [
    "breakout_volume_ok",
    "close_lower_locked",
    "entry_day_lows",
    "first_trigger",
    "full_day_pace_skip",
    "inferred_band",
    "lower_circuit_locked",
    "stop_order_fill",
    "pre_break_low",
    "upper_circuit_locked",
]

# The NSE price bands an upper circuit is inferred at when the run has no band table (PR 61).
NSE_BANDS = (Decimal(2), Decimal(5), Decimal(10), Decimal(20))
# `volume_pace` on the full day's volume: too little (live's reason), or the volumes are missing
SKIP_PACE_LOW = trigger.PACE_LOW
SKIP_PACE_MISSING = "volume_pace_missing"
# a low-of-day stop on a daily estimate whose session gapped over the level: the low before the
# break is the opening range's, which a daily bar does not have
SKIP_GAP_NO_INTRADAY = "gap_over_no_intraday"


def _acts(rules: Sequence[Any], state: exits.PositionState, price: Decimal) -> bool:
    result = exits.evaluate(rules, state, exits.ExitView(price), "live")
    return bool(result.actions or result.new_stop is not None or result.fired)


def pre_break_low(
    bar_open: Decimal, bar_low: Decimal, level: Decimal, adr20: Decimal | None
) -> tuple[Decimal, Decimal | None]:
    """(the day's low before an entry at `level` broke, its low after the break or None) on an
    entry day estimated from its daily bar (HAT-BT-020, PR 60; a session with 5-minute bars reads
    them, `intraday.after_trigger`): max(day low, min(open, level x (1 - adr20 / 100))), and the
    day low as the low after the break when it is below that; with no `adr20` the whole day's
    low comes before the break."""
    if adr20 is None or adr20 < 0:
        return bar_low, None
    estimate = min(bar_open, level * (1 - adr20 / 100))
    before = max(bar_low, estimate)
    return before, (bar_low if bar_low < before else None)


def stop_order_fill(
    open_: Decimal,
    high: Decimal,
    level: Decimal,
    tick: Decimal | None,
    slippage: Decimal = STOP_SLIPPAGE,
) -> Decimal | None:
    """The daily estimate of a buy stop-limit at `level` resting from 09:20 (`entry_timing:
    stop_order`, decisions.md "Setup entry orders"): a session that opens above the level buys
    at 09:20 (`fill_0920.gap_over_fill`); one whose high reaches the stop's trigger (the first
    tick above the level, `trigger.first_above`) buys at the trigger moved up by `slippage` (a
    fraction), up to the tick and at most the high. That premium stands for the stop's
    slippage and for a break between 09:15 and 09:20, which buys at the 09:20 price. None when
    the high falls short or without a positive tick. `max_chase` judges the price after."""
    if open_ > level:
        return gap_over_fill(open_, high)
    if tick is None or tick <= 0:
        return None
    stop = trigger.first_above(level, tick)
    if high < stop:
        return None
    moved = (stop * (1 + slippage) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    return min(high, moved)


def entry_day_lows(
    bar: tuple[Decimal, Decimal],
    level: Decimal,
    price: Decimal,
    adr20: Decimal | None,
    *,
    low_of_day: bool,
) -> tuple[Decimal, Decimal | None] | str:
    """(the entry day's low before the break, its low after the entry or None) of a daily
    estimate entry at `price` breaking `level` on a session whose (open, low) is `bar`, or the
    skip reason (HAT-BT-020, PR 60). A session that opened above the level gapped over it,
    whatever it filled at (the 09:20 price is not the open): a low-of-day stop then needs the
    opening range's low (`gap_over_no_intraday`), any other stop sees the whole day's low after
    the fill. Otherwise the low before the break is `pre_break_low`'s at the entry price, and a
    low-of-day stop without `adr20` is refused (`stop_input_missing`): it would read the whole day's
    low before the break."""
    bar_open, bar_low = bar
    gapped = bar_open > level
    if low_of_day:
        if gapped:
            return SKIP_GAP_NO_INTRADAY
        if adr20 is None or adr20 < 0:
            return SKIP_BAD_INPUT
    before, after = pre_break_low(bar_open, bar_low, level if gapped else price, adr20)
    return (before, bar_low) if gapped else (before, after)


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


def inferred_band(
    high: Decimal, prev_close: Decimal | None, tick: Decimal | None
) -> Decimal | None:
    """The price band of a session replayed on 5-minute bars, which the run has no table of: the
    narrowest NSE band whose upper price (`trigger.upper_circuit_price`) is above the previous
    close and within one tick of the day's `high`, a high above the previous close (the session
    reached its circuit), else None, read as no band (nothing in the session was at a circuit).
    None without a previous close or a tick."""
    if prev_close is None or prev_close <= 0 or tick is None or tick <= 0 or high <= prev_close:
        return None
    for band in NSE_BANDS:
        daily = trigger.DailyInputs(
            prev_close=prev_close, tick=tick, band_known=True, band_pct=band
        )
        upper = trigger.upper_circuit_price(daily)
        if upper is not None and upper > prev_close and abs(high - upper) <= tick:
            return band
    return None


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
    if not open_ == high == low == close:
        return False
    return _at_lower_band(close, prev_close, tick)


def close_lower_locked(
    close: Decimal, low: Decimal, prev_close: Decimal | None, tick: Decimal | None
) -> bool | None:
    """Whether a session closed locked at its lower circuit (it reached the band and stayed: the
    close is the day's low, within one tick of an NSE band's lower price under the previous
    close), read as `lower_circuit_locked` reads a whole day: a sale at that close (a close
    rule's, live's 15:15 one) found no buyer. None without a previous close or a tick."""
    if prev_close is None or prev_close <= 0 or tick is None or tick <= 0:
        return None
    return close == low and _at_lower_band(close, prev_close, tick)


def _at_lower_band(close: Decimal, prev_close: Decimal, tick: Decimal) -> bool:
    if close >= prev_close:
        return False
    for band in NSE_BANDS:
        daily = trigger.DailyInputs(
            prev_close=prev_close, tick=tick, band_known=True, band_pct=band
        )
        lower = trigger.lower_circuit_price(daily)
        if lower is not None and lower < prev_close and abs(close - lower) <= tick:
            return True
    return False


def breakout_volume_ok(volumes: Sequence[float], days: int, multiple: float) -> bool | None:
    """Whether the last of `volumes` (the breakout session's, known at its close) is at least
    `multiple` x the mean of the `days` before it, compared exactly without dividing; None when
    those `days + 1` volumes are not all there, or the mean is not positive."""
    if len(volumes) != days + 1 or any(not math.isfinite(v) or v < 0 for v in volumes):
        return None
    exact = [require_exact(v, "volume", floats=True) for v in volumes]
    prior = sum(exact[:-1], Decimal(0))
    if prior <= 0:
        return None
    return exact[-1] * days >= require_exact(multiple, "multiple", floats=True) * prior


def full_day_pace_skip(volumes: Sequence[float], multiple: float) -> str | None:
    """`volume_pace` judged on the full day's volume (a look-ahead: live knows only the volume
    up to its trigger): None when the last of `volumes`, the day's, is at least `multiple` x the
    mean of the `AVG_VOLUME_DAYS` sessions before it (live's 50-day average through the previous
    session), else the skip reason, `volume_pace_missing` when those volumes are not all there."""
    ok = breakout_volume_ok(volumes, trigger.AVG_VOLUME_DAYS, multiple)
    if ok is None:
        return SKIP_PACE_MISSING
    return None if ok else SKIP_PACE_LOW


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
