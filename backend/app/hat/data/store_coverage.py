"""What the store holds against what it should: the rows per expected day of a dataset and the
index days missing (HAT-DATA-091, 095, 096), for the history build's report."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date

from .store import DATASET_BARS, DATASET_INDEX, DataStore, StoreError

_COUNT_SQL = {
    DATASET_BARS: "SELECT date, count(*) FROM bars_daily_raw GROUP BY date ORDER BY date",
    DATASET_INDEX: "SELECT date, count(*) FROM index_daily GROUP BY date ORDER BY date",
}


@dataclass
class PartitionReport:
    """Which expected days have data (HAT-DATA-091, 096). A missing day stays missing."""

    row_counts: dict[date, int] = field(default_factory=dict)
    missing: list[date] = field(default_factory=list)
    unexpected: list[date] = field(default_factory=list)  # data on a day that is not expected


def partition_report(
    store: DataStore, dataset: str, expected_days: Collection[date]
) -> PartitionReport:
    """Row count per day found, the expected days with no rows (missing) and the days with data
    that are not expected. Missing days are reported and left empty (HAT-DATA-091)."""
    report = PartitionReport()
    con = store.connect()
    try:
        for day, count in con.execute(_COUNT_SQL[dataset]).fetchall():
            report.row_counts[day] = count
    finally:
        con.close()
    expected = set(expected_days)
    report.missing = sorted(d for d in expected if report.row_counts.get(d, 0) == 0)
    report.unexpected = sorted(d for d in report.row_counts if d not in expected)
    return report


def index_coverage(
    store: DataStore, trading_days: Collection[date], starts: dict[str, date]
) -> dict[str, list[date]]:
    """Per index, the trading days on or after its start date that have no row (HAT-DATA-095).
    Raises when an index has more than one row on a day."""
    con = store.connect()
    try:
        counts = {
            (name, day): n
            for name, day, n in con.execute(
                "SELECT index_name, date, count(*) FROM index_daily GROUP BY index_name, date"
            ).fetchall()
        }
    finally:
        con.close()
    if any(n > 1 for n in counts.values()):
        raise StoreError("store_duplicate_key")
    return {
        name: sorted(d for d in trading_days if d >= start and (name, d) not in counts)
        for name, start in starts.items()
    }
