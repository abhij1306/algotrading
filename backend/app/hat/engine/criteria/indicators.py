"""Exact window arithmetic shared by the daily criteria (definitions on adjusted bars).

A window is the last `n` values of a series and is usable only when it is full and has no null,
as for the feature columns. Sums are `Decimal` over `dec(value)`, so every comparison is exact.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from .base import dec


def last(values: Sequence[float | None], n: int, *, offset: int = 0) -> list[float] | None:
    """The `n` values ending `offset` rows before the last, or None unless that window exists and
    holds no null."""
    end = len(values) - offset
    if n < 1 or end < n or offset < 0:
        return None
    chunk = values[end - n : end]
    if any(v is None for v in chunk):
        return None
    return [float(v) for v in chunk if v is not None]


def dsum(values: Sequence[float]) -> Decimal:
    return sum((dec(v) for v in values), Decimal(0))


def sma_sum(close: Sequence[float | None], n: int, *, offset: int = 0) -> Decimal | None:
    """The sum of the `n` closes ending `offset` rows back: the mean is this over `n`."""
    window = last(close, n, offset=offset)
    return None if window is None else dsum(window)


def mean_gt(sum_a: Decimal, n_a: int, sum_b: Decimal, n_b: int) -> bool:
    """sum_a / n_a > sum_b / n_b, exactly (no division)."""
    return sum_a * n_b > sum_b * n_a


def traded_values(
    traded_value: Sequence[float | None],
    close: Sequence[float | None],
    volume: Sequence[float | None],
) -> list[float | None]:
    """Traded value per bar: the reported one, or close x volume where it is null."""
    out: list[float | None] = []
    for tv, c, v in zip(traded_value, close, volume, strict=True):
        if tv is not None:
            out.append(tv)
        elif c is not None and v is not None:
            out.append(float(dec(c) * dec(v)))
        else:
            out.append(None)
    return out


def daily_range_pct(
    high: Sequence[float | None], low: Sequence[float | None]
) -> list[Decimal | None]:
    """100 x (high / low - 1) per bar, None where a price is missing or the low is not positive."""
    out: list[Decimal | None] = []
    for h, lo in zip(high, low, strict=True):
        if h is None or lo is None or lo <= 0:
            out.append(None)
        else:
            out.append(100 * (dec(h) / dec(lo) - 1))
    return out
