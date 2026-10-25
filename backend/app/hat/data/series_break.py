"""A series break: an instrument that comes back after more than `SERIES_BREAK_DAYS` calendar
days without a bar starts a new series, so no feature or criteria window spans the hole (the
windows are null, or the history short, until they refill on the new rows). The gap is counted in
calendar days between the instrument's own consecutive bars, so it is a function of two rows and
reads the same in a full rebuild, an incremental run and a scan.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import polars as pl

SERIES_BREAK_DAYS = 92  # one quarter, about 63 sessions (decision in docs/decisions.md)


def is_break(previous: date | None, day: date) -> bool:
    """`day` starts a new series after a bar on `previous` (no previous bar: not a break)."""
    return previous is not None and (day - previous).days > SERIES_BREAK_DAYS


def breaks(dates: pl.Expr) -> pl.Expr:
    """Per row, whether it starts a new series after the row before (over a frame sorted by date
    within the window's partition; the first row is not a break)."""
    return (dates.diff().dt.total_days() > SERIES_BREAK_DAYS).fill_null(value=False)


def segment_start(dates: Sequence[date], until: date | None = None) -> int:
    """The index of the first of `dates` (ascending) in the series of the last one, or of
    `until` (a later day with no bar yet): `len(dates)` when `until` itself starts a new one."""
    if until is not None and dates and is_break(dates[-1], until):
        return len(dates)
    for n in range(len(dates) - 1, 0, -1):
        if is_break(dates[n - 1], dates[n]):
            return n
    return 0
