"""Persist final 5-minute bars to `bars_5m/date=YYYY-MM-DD/` Parquet (HAT-DATA-117).

One file per trade date, rewritten whole through a temporary file and an atomic replace, so a
reader sees the old or the new content, never a partial file (the same rule as HAT-DATA-082).
Rows are keyed by (instrument_id, bar_start): writing the same bar again changes nothing, which
makes the writer safe to re-run after a crash. Only the layout of 02-data §8 is written:
`instrument_id, bar_start, open, high, low, close, volume`.

Beside it, `coverage.json` records the session coverage a restart needs (PR 22): the instruments
the writer watched from the open and the start of the day's first feed gap. It only grows (a
union and a minimum), and it is written after the bars, so a crash between the two leaves less
coverage, never more: a restored opening range then fails closed.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ..time import as_utc, trade_date_of
from .bars import Bar

__all__ = ["DATASET", "SCHEMA", "BarRow", "BarStore", "Coverage"]

DATASET = "bars_5m"
PRICE = pa.decimal128(18, 4)
SCHEMA = pa.schema(
    [
        ("instrument_id", pa.string()),
        ("bar_start", pa.timestamp("us", tz="UTC")),
        ("open", PRICE),
        ("high", PRICE),
        ("low", PRICE),
        ("close", PRICE),
        ("volume", pa.int64()),
    ]
)
FILE_NAME = "bars.parquet"
COVERAGE_NAME = "coverage.json"


@dataclass(frozen=True)
class BarRow:
    instrument_id: str
    bar_start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    @property
    def key(self) -> tuple[str, datetime]:
        return (self.instrument_id, self.bar_start)


def row_from_bar(bar: Bar, instrument_id: uuid.UUID | str) -> BarRow:
    return BarRow(
        str(instrument_id),
        as_utc(bar.start),
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
    )


@dataclass(frozen=True)
class Coverage:
    """One day's session coverage: instruments watched from the open, first feed gap."""

    from_open: frozenset[str] = frozenset()
    gap_from: datetime | None = None


class BarStore:
    """Reads and writes the `bars_5m` dataset under `root` (the HAT data root)."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def partition(self, day: date) -> Path:
        return self._root / DATASET / f"date={day.isoformat()}"

    def path(self, day: date) -> Path:
        return self.partition(day) / FILE_NAME

    def read(self, day: date) -> list[BarRow]:
        """Every stored row of `day`, ordered by (bar_start, instrument_id)."""
        path = self.path(day)
        if not path.is_file():
            return []
        table = pq.read_table(path, schema=SCHEMA)
        rows = [
            BarRow(
                str(r["instrument_id"]),
                as_utc(r["bar_start"]),
                r["open"],
                r["high"],
                r["low"],
                r["close"],
                int(r["volume"]),
            )
            for r in table.to_pylist()
        ]
        return sorted(rows, key=lambda r: (r.bar_start, r.instrument_id))

    def write(self, bars: Iterable[Bar], instrument_ids: Mapping[str, uuid.UUID | str]) -> int:
        """Merge `bars` into their trade dates' partitions; return the rows that were new or
        changed. A bar whose symbol has no instrument id is skipped (nothing is invented)."""
        by_day: dict[date, dict[tuple[str, datetime], BarRow]] = {}
        for bar in bars:
            instrument_id = instrument_ids.get(bar.symbol)
            if instrument_id is None:
                continue
            row = row_from_bar(bar, instrument_id)
            by_day.setdefault(trade_date_of(bar.start), {})[row.key] = row
        written = 0
        for day, fresh in sorted(by_day.items()):
            merged = {row.key: row for row in self.read(day)}
            changed = sum(1 for key, row in fresh.items() if merged.get(key) != row)
            if changed:
                merged.update(fresh)
                self._replace(day, list(merged.values()))
            written += changed
        return written

    def read_coverage(self, day: date) -> Coverage:
        """The stored coverage of `day`; none (nothing watched from the open) when absent."""
        path = self.partition(day) / COVERAGE_NAME
        if not path.is_file():
            return Coverage()
        raw = json.loads(path.read_text(encoding="utf-8"))
        gap = raw.get("gap_from")
        return Coverage(
            frozenset(str(i) for i in raw.get("from_open", ())),
            None if gap is None else as_utc(datetime.fromisoformat(gap)),
        )

    def write_coverage(self, day: date, coverage: Coverage) -> None:
        """Merge `coverage` into the stored one: the union of the instruments, the earlier
        gap. Written whole through a temporary file and an atomic replace."""
        stored = self.read_coverage(day)
        gaps = [g for g in (stored.gap_from, coverage.gap_from) if g is not None]
        merged = Coverage(stored.from_open | coverage.from_open, min(gaps, default=None))
        if merged == stored and self.partition(day).joinpath(COVERAGE_NAME).is_file():
            return
        body = {
            "from_open": sorted(merged.from_open),
            "gap_from": None if merged.gap_from is None else as_utc(merged.gap_from).isoformat(),
        }
        self._atomic(self.partition(day) / COVERAGE_NAME, json.dumps(body).encode())

    def _atomic(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_bytes(data)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def _replace(self, day: date, rows: list[BarRow]) -> None:
        rows = sorted(rows, key=lambda r: (r.bar_start, r.instrument_id))
        table = pa.table(
            {
                "instrument_id": [r.instrument_id for r in rows],
                "bar_start": [r.bar_start.astimezone(UTC) for r in rows],
                "open": [r.open for r in rows],
                "high": [r.high for r in rows],
                "low": [r.low for r in rows],
                "close": [r.close for r in rows],
                "volume": [r.volume for r in rows],
            },
            schema=SCHEMA,
        )
        target = self.path(day)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{FILE_NAME}.{uuid.uuid4().hex}.tmp")
        try:
            pq.write_table(table, temp)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
