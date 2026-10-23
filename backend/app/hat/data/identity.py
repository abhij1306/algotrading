"""One instrument per ISIN (audit D5, P6b): the pure rules of the identity.

An instrument is (market, ISIN). Its series (EQ, BE, BZ, ...) is an attribute: the instrument
master holds the series it trades under now (and since when), and each `bars_daily_raw` row the
series that traded that day. NSE lists one ISIN under at most one normal-market series a day, but
a block-deal (BL), institutional-placement (IL) or similar window can trade beside it; those rows
are not the day's market, so the day keeps the row of the best-ranked series.
"""

from __future__ import annotations

import uuid
from bisect import bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TypeVar

__all__ = [
    "Interval",
    "Sibling",
    "best_per_isin",
    "kept_id",
    "merge_symbol_history",
    "series_as_of",
    "series_changes",
    "series_on",
    "series_rank",
]

# The normal-market series, best first; every other series ranks after them, by name.
MARKET_SERIES = ("EQ", "BE", "BZ")

T = TypeVar("T")


def series_rank(series: str) -> tuple[int, str]:
    """Sort key of a series: EQ, BE, BZ, then the rest by name."""
    try:
        return (MARKET_SERIES.index(series), "")
    except ValueError:
        return (len(MARKET_SERIES), series)


def best_per_isin(
    rows: Iterable[T], isin: Callable[[T], str], series: Callable[[T], str]
) -> list[T]:
    """One row per ISIN, the best-ranked series' (the first one seen on a tie), in first-seen
    order of the ISINs."""
    best: dict[str, T] = {}
    for row in rows:
        key = isin(row)
        held = best.get(key)
        if held is None or series_rank(series(row)) < series_rank(series(held)):
            best[key] = row
    return list(best.values())


def series_changes(rows: Iterable[tuple[str, date, str]]) -> dict[str, list[tuple[date, str]]]:
    """Each instrument's series as change points (first day, series), oldest first, from its
    bars' (instrument id, day, series)."""
    out: dict[str, list[tuple[date, str]]] = {}
    for instrument_id, day, series in sorted(rows):
        points = out.setdefault(instrument_id, [])
        if not points or points[-1][1] != series:
            points.append((day, series))
    return out


def series_on(
    changes: Mapping[str, Sequence[tuple[date, str]]], instrument_id: str, day: date
) -> str | None:
    """The series the instrument traded under on `day` (its last bar's on or before it), or
    None before its first bar."""
    points = changes.get(instrument_id, ())
    at = bisect_right(points, day, key=lambda p: p[0])
    return points[at - 1][1] if at else None


def series_as_of(changes: Mapping[str, Sequence[tuple[date, str]]], day: date) -> dict[str, str]:
    """`series_on` of every instrument that had traded by `day`."""
    found = ((i, series_on(changes, i, day)) for i in changes)
    return {i: s for i, s in found if s is not None}


@dataclass(frozen=True)
class Sibling:
    """One of the instrument rows an ISIN had under the (isin, series) identity."""

    id: uuid.UUID
    series: str
    active: bool


def kept_id(siblings: Sequence[Sibling]) -> uuid.UUID:
    """The id an ISIN keeps: the active row (at most one is active), else the best-ranked
    series, else the lowest id."""
    return min(siblings, key=lambda s: (not s.active, series_rank(s.series), s.id.bytes)).id


@dataclass(frozen=True)
class Interval:
    """A symbol held over [valid_from, valid_to); `valid_to` None is open-ended."""

    symbol: str
    valid_from: date
    valid_to: date | None


def merge_symbol_history(histories: Sequence[Sequence[Interval]]) -> list[Interval]:
    """The symbol history of the merged instrument from its siblings' histories (the kept
    sibling's first). Each history is disjoint; together they may overlap, because a row created
    by a listing refresh claims the symbol from the listing date. On each day the covering
    interval that began latest wins (it was recorded by a rename or a relisting, so it is the more
    specific), then the one that ends first, then the earlier history. Days no history covers
    stay a gap; adjacent pieces of one symbol are joined."""
    ranked = [(iv, order) for order, history in enumerate(histories) for iv in history if _span(iv)]
    bounds = sorted({b for iv, _ in ranked for b in (iv.valid_from, _end(iv))})
    out: list[Interval] = []
    for start, stop in zip(bounds, bounds[1:], strict=False):
        cover = [(iv, order) for iv, order in ranked if iv.valid_from <= start and stop <= _end(iv)]
        if not cover:
            continue
        iv, _ = min(cover, key=lambda c: (-c[0].valid_from.toordinal(), _end(c[0]), c[1]))
        end = None if stop == date.max else stop
        last = out[-1] if out else None
        if last is not None and last.symbol == iv.symbol and last.valid_to == start:
            out[-1] = Interval(iv.symbol, last.valid_from, end)
        else:
            out.append(Interval(iv.symbol, start, end))
    return out


def _end(iv: Interval) -> date:
    return date.max if iv.valid_to is None else iv.valid_to


def _span(iv: Interval) -> bool:
    return iv.valid_from < _end(iv)
