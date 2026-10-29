"""The rotation strategy's rules (`strategy.config.Rotation`), as pure functions of plain data so
the backtest and a live sleeve read the same ones: the rebalance calendar, the cross-sectional
score, the selection with its hold buffer and the order diff to equal target weights.

A rebalance is decided on the close of its session (`is_rebalance`) and traded at the next
session's open. The score reads each name's total-return closes over a window ending on the
decision session, forward-filled over at most `FFILL_SESSIONS` sessions without a bar
(`scores`); `riskadj` is a cross-sectional z-score (`zscores`) over the names scored that night,
so a name's score depends on the others'. `select` keeps a holding while it ranks within the
buffer and fills the rest best first; `order_diff` turns the target list into sells, trims, buys
and top-ups at whole shares, skipping a change under the threshold.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal

import numpy as np

from .strategy.config import Rotation

__all__ = [
    "BUY",
    "SELL",
    "TOP_UP",
    "TRIM",
    "Order",
    "is_rebalance",
    "order_diff",
    "ranked",
    "riskadj_parts",
    "scores",
    "select",
    "window_rows",
    "zscores",
]

FFILL_SESSIONS = 10  # a close carries over at most this many sessions without a bar
SESSIONS_PER_YEAR = 252  # annualises the volatility
SELL, TRIM, BUY, TOP_UP = "sell", "trim", "buy", "top_up"
_HUNDRED = Decimal(100)


def is_rebalance(night: date, next_session: date, cadence: str) -> bool:
    """Whether `night`, the session before `next_session`, closes a rebalance period: the last
    session of its month (`month_end`), also the last of its first half (`fortnight`: on or
    before the 15th, the next after it), or the last of its ISO week (`weekly`)."""
    month_end = (night.year, night.month) != (next_session.year, next_session.month)
    if cadence == "month_end":
        return month_end
    if cadence == "fortnight":
        return month_end or night.day <= 15 < next_session.day
    if cadence == "weekly":
        return night.isocalendar()[:2] != next_session.isocalendar()[:2]
    raise ValueError("unknown_rebalance")


def window_rows(rotation: Rotation) -> int:
    """The rows of closes `scores` reads, ending on the decision session: the longest lookback
    and the rows a close may be carried over from before it."""
    return max(rotation.long_sessions, rotation.vol_sessions) + 1 + FFILL_SESSIONS


def _ffill(closes: np.ndarray, limit: int) -> np.ndarray:
    """`closes` (rows sessions, columns names; NaN without a bar) with each NaN replaced by the
    last close at most `limit` rows before it."""
    out = closes.copy()
    last = np.full(closes.shape[1], np.nan)
    gap = np.zeros(closes.shape[1], dtype=np.int64)
    for row in range(closes.shape[0]):
        seen = ~np.isnan(closes[row])
        last = np.where(seen, closes[row], last)
        gap = np.where(seen, 0, gap + 1)
        out[row] = np.where(seen, closes[row], np.where(gap <= limit, last, np.nan))
    return out


def _single(closes: np.ndarray, rotation: Rotation) -> np.ndarray:
    """The score of a one-ratio rule (`r6`, `r12_1`, `hi52`) per name, NaN where unscored."""
    filled, ok = _prepared(closes, rotation)
    last = filled[-1]
    long_back = filled[-1 - rotation.long_sessions]
    with np.errstate(divide="ignore", invalid="ignore"):
        if rotation.score == "r6":
            found = last / filled[-1 - rotation.short_sessions] - 1
        elif rotation.score == "r12_1":
            found = filled[-1 - rotation.skip_sessions] / long_back - 1
        else:  # hi52: the close over its high, to 1%, ties broken by the 12-1 return
            high = np.nanmax(_tail(filled, rotation.long_sessions), axis=0)
            r12_1 = filled[-1 - rotation.skip_sessions] / long_back - 1
            found = np.round(last / high, 2) + 1e-5 * np.clip(r12_1, -1, 10)
    return np.where(ok & np.isfinite(found), found, np.nan)


def riskadj_parts(closes: np.ndarray, rotation: Rotation) -> tuple[np.ndarray, np.ndarray]:
    """(short return / volatility, long return / volatility) per name, NaN where unscored: the
    returns over `short_sessions` and `long_sessions`, the volatility the annualised sample
    deviation of the last `vol_sessions` daily log returns."""
    filled, ok = _prepared(closes, rotation)
    with np.errstate(divide="ignore", invalid="ignore"):
        last = filled[-1]
        short = last / filled[-1 - rotation.short_sessions] - 1
        long_ = last / filled[-1 - rotation.long_sessions] - 1
        returns = np.diff(np.log(_tail(filled, rotation.vol_sessions + 1)), axis=0)
        counts = np.sum(~np.isnan(returns), axis=0)
        vol = np.full(closes.shape[1], np.nan)
        enough = counts >= 2
        if enough.any():
            vol[enough] = np.nanstd(returns[:, enough], axis=0, ddof=1) * math.sqrt(
                SESSIONS_PER_YEAR
            )
        good = ok & np.isfinite(vol) & (vol > 0)
        return (
            np.where(good & np.isfinite(short), short / vol, np.nan),
            np.where(good & np.isfinite(long_), long_ / vol, np.nan),
        )


def _tail(rows: np.ndarray, count: int) -> np.ndarray:
    return rows[max(0, rows.shape[0] - count) :]


def _prepared(closes: np.ndarray, rotation: Rotation) -> tuple[np.ndarray, np.ndarray]:
    """(the closes forward-filled, which names have `min_bars` bars in the long window)."""
    if closes.shape[0] < window_rows(rotation):
        raise ValueError("rotation_window_short")
    bars = np.sum(~np.isnan(_tail(closes, rotation.long_sessions)), axis=0)
    return _ffill(closes, FFILL_SESSIONS), bars >= rotation.min_bars


def zscores(values: np.ndarray) -> np.ndarray:
    """Each value's distance from the mean of the finite values in sample deviations (NaN for
    a NaN value, or all NaN with fewer than two values or no spread)."""
    finite = values[np.isfinite(values)]
    if finite.size < 2:
        return np.full(values.shape, np.nan)
    spread = float(np.std(finite, ddof=1))
    if spread == 0:
        return np.full(values.shape, np.nan)
    return (values - float(np.mean(finite))) / spread


def scores(closes: np.ndarray, rotation: Rotation) -> np.ndarray:
    """The night's score of each column of `closes` (NaN: not ranked), the total-return closes
    of the last `window_rows` sessions (NaN without a bar), the decision session last: `riskadj`
    the mean of the z-scores of its two ratios across the names scored that night (the columns
    are the night's eligible names), the others their one ratio. A name needs `min_bars` bars
    among the last `long_sessions` sessions."""
    if rotation.score != "riskadj":
        return _single(closes, rotation)
    short, long_ = riskadj_parts(closes, rotation)
    return 0.5 * zscores(short) + 0.5 * zscores(long_)


def ranked(names: Sequence[str], values: np.ndarray) -> list[tuple[str, float]]:
    """The scored names, best first (ties by name), with their scores; a NaN is left out."""
    found = [(n, float(v)) for n, v in zip(names, values, strict=True) if math.isfinite(v)]
    return sorted(found, key=lambda pair: (-pair[1], pair[0]))


def select(
    ranking: Sequence[str],
    held: Collection[str],
    top_n: int,
    buffer_rank: int,
    can_buy: Callable[[str], bool],
    kept: Sequence[str] = (),
) -> list[str]:
    """The target names: `kept` first (held names kept without a rank), then each holding
    ranked within `buffer_rank` (1 the best of `ranking`), best first, then the best names
    `ranking` holds that are not targets yet and `can_buy`, up to `top_n` in all. A holding
    outside the buffer, or not ranked, is not a target."""
    target = list(kept)[:top_n]
    rank = {name: n for n, name in enumerate(ranking, start=1)}
    stay = sorted((h for h in held if rank.get(h, buffer_rank + 1) <= buffer_rank), key=rank.get)
    for name in stay:
        if len(target) >= top_n:
            break
        if name not in target:
            target.append(name)
    for name in ranking:
        if len(target) >= top_n:
            break
        if name not in target and name not in held and can_buy(name):
            target.append(name)
    return target


@dataclass(frozen=True)
class Order:
    instrument_id: str
    action: str  # SELL, TRIM, BUY or TOP_UP
    qty: int


def _shares(value: Decimal, price: Decimal) -> int:
    return int((value / price).to_integral_value(ROUND_FLOOR))


def order_diff(
    target: Sequence[str],
    held: Mapping[str, int],
    prices: Mapping[str, Decimal],
    capital: Decimal,
    top_n: int,
    min_change_pct: Decimal,
    *,
    reweight: bool,
) -> list[Order]:
    """The orders that take `held` (shares by name) to `target` at `prices`: a sale of every
    holding not a target, then each target's change to `capital` / `top_n` of value, in whole
    shares. A change of at most `min_change_pct` percent of that value is skipped; without
    `reweight` a held target is left as it is. Sales and trims come first, then buys and
    top-ups in target order. A target without a price gets no order."""
    sells = [Order(name, SELL, qty) for name, qty in held.items() if qty > 0 and name not in target]
    each = capital / top_n if top_n > 0 else Decimal(0)
    floor = each * min_change_pct / _HUNDRED
    trims: list[Order] = []
    buys: list[Order] = []
    for name in target:
        price = prices.get(name)
        if price is None or price <= 0 or each <= 0:
            continue
        qty = held.get(name, 0)
        change = each - qty * price
        if qty == 0:
            if change > floor and (shares := _shares(change, price)) > 0:
                buys.append(Order(name, BUY, shares))
        elif reweight and -change > floor and (shares := _shares(-change, price)) > 0:
            trims.append(Order(name, TRIM, min(shares, qty)))
        elif reweight and change > floor and (shares := _shares(change, price)) > 0:
            buys.append(Order(name, TOP_UP, shares))
    return [*sells, *trims, *buys]
