"""Persist final 5-minute bars to `bars_5m/date=YYYY-MM-DD/` Parquet (HAT-DATA-117).

One file per trade date, rewritten whole through a temporary file and an atomic replace, so a
reader sees the old or the new content, never a partial file (the same rule as HAT-DATA-082).
Rows are keyed by (instrument_id, bar_start): writing the same bar again changes nothing, which
makes the writer safe to re-run after a crash. Only the layout is written:
`instrument_id, bar_start, open, high, low, close, volume`.

The minute-history download (audit D1, D2) replaces an instrument's stored days over the period
it loaded (`write(..., replace=...)`), so a day it refused leaves no bar of an older basis behind,
and records what it refused in the append-only ledger `bars_5m_rejects/` (one Parquet file per
write: instrument, day, 5-minute bucket and the rule; outside `bars_5m`, so no reader of the
day partitions sees it).

Beside it, `coverage.json` records the session coverage a restart needs (PR 22): the instruments
the writer watched from the open and the start of the day's first feed gap. It only grows (a
union and a minimum), and it is written after the bars, so a crash between the two leaves less
coverage, never more: a restored opening range then fails closed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ..time import as_utc, trade_date_of
from .bars import Bar
from .store import atomic_replace

__all__ = ["DATASET", "REJECTS", "SCHEMA", "BarStore", "Coverage", "RejectRow"]

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
REJECTS = "bars_5m_rejects"
REJECT_SCHEMA = pa.schema(
    [
        ("instrument_id", pa.string()),
        ("symbol", pa.string()),
        ("day", pa.date32()),
        ("bar_start", pa.timestamp("us", tz="UTC")),
        ("rule", pa.string()),
    ]
)
FILE_NAME = "bars.parquet"
COVERAGE_NAME = "coverage.json"


@dataclass(frozen=True)
class BarRow5m:
    """One stored 5-minute bar (this module's own row; the daily row is `store.BarRow`)."""

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


@dataclass(frozen=True)
class RejectRow:
    """One refusal in the ledger: a candle's 5-minute bucket (`bar_start`, None when the
    candle could not be placed) or a whole day (`bar_start` None), and the rule that refused
    it."""

    instrument_id: str
    symbol: str
    day: date | None
    bar_start: datetime | None
    rule: str


def row_from_bar(bar: Bar, instrument_id: uuid.UUID | str) -> BarRow5m:
    return BarRow5m(
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
    """One day's session coverage: instruments watched from the open, first feed gap, and how
    far the feed was seen (up to the first bar's end)."""

    from_open: frozenset[str] = frozenset()
    gap_from: datetime | None = None
    seen_until: datetime | None = None


class BarStore:
    """Reads and writes the `bars_5m` dataset under `root` (the HAT data root)."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def partition(self, day: date) -> Path:
        return self._root / DATASET / f"date={day.isoformat()}"

    def path(self, day: date) -> Path:
        return self.partition(day) / FILE_NAME

    def read(self, day: date) -> list[BarRow5m]:
        """Every stored row of `day`, ordered by (bar_start, instrument_id)."""
        return sorted(_rows(self._table(day)), key=lambda r: (r.bar_start, r.instrument_id))

    def _table(self, day: date) -> pa.Table:
        path = self.path(day)
        return pq.read_table(path, schema=SCHEMA) if path.is_file() else SCHEMA.empty_table()

    def days(self) -> list[date]:
        """The trade dates that have a stored file, oldest first."""
        base = self._root / DATASET
        if not base.is_dir():
            return []
        return sorted(
            date.fromisoformat(p.name[5:])
            for p in base.glob("date=*")
            if p.joinpath(FILE_NAME).is_file()
        )

    def write(
        self,
        bars: Iterable[Bar],
        instrument_ids: Mapping[str, uuid.UUID | str],
        *,
        replace: Mapping[str, tuple[date, date]] | None = None,
    ) -> int:
        """Merge `bars` into their trade dates' partitions; return the rows that were new or
        changed. A bar whose symbol has no instrument id is skipped (nothing is invented).
        `replace` (instrument id -> first and last day) drops every stored row of the instrument
        on those days first, so the bars given are then all it has there. Each day file is
        rewritten once per call whatever the number of instruments (the minute-history download
        writes a batch of symbols at once, D7). Only the stored rows of the written instruments
        are turned into Python rows; the rest of the day is carried over as Arrow data."""
        by_day: dict[date, dict[tuple[str, datetime], BarRow5m]] = {}
        for bar in bars:
            instrument_id = instrument_ids.get(bar.symbol)
            if instrument_id is None:
                continue
            row = row_from_bar(bar, instrument_id)
            by_day.setdefault(trade_date_of(bar.start), {})[row.key] = row
        spans = {str(i): span for i, span in (replace or {}).items()}
        days = set(by_day)
        if spans:
            days |= {d for d in self.days() if any(a <= d <= b for a, b in spans.values())}
        written = 0
        for day in sorted(days):
            fresh = by_day.get(day, {})
            cleared = {i for i, (a, b) in spans.items() if a <= day <= b}
            stored = self._table(day)
            ids = pa.array(sorted({key[0] for key in fresh} | cleared), pa.string())
            mine = pc.is_in(stored["instrument_id"], value_set=ids)
            old = {row.key: row for row in _rows(stored.filter(mine))}
            merged = {key: row for key, row in old.items() if key[0] not in cleared}
            merged.update(fresh)
            if merged != old:
                self._replace(day, stored.filter(pc.invert(mine)), list(merged.values()))
            written += sum(1 for key, row in fresh.items() if old.get(key) != row)
        return written

    def write_rejects(self, rows: Iterable[RejectRow]) -> None:
        """Append `rows` to the reject ledger as one new file (nothing when there are none)."""
        listed = list(rows)
        if not listed:
            return
        table = pa.table(
            {
                "instrument_id": [r.instrument_id for r in listed],
                "symbol": [r.symbol for r in listed],
                "day": [r.day for r in listed],
                "bar_start": [None if r.bar_start is None else as_utc(r.bar_start) for r in listed],
                "rule": [r.rule for r in listed],
            },
            schema=REJECT_SCHEMA,
        )
        target = self._root / REJECTS / f"part-{uuid.uuid4().hex}.parquet"
        self._atomic_table(target, table)

    def read_rejects(self) -> list[RejectRow]:
        """Every ledger row, in no particular order."""
        rows = []
        for path in sorted((self._root / REJECTS).glob("part-*.parquet")):
            for r in pq.read_table(path, schema=REJECT_SCHEMA).to_pylist():
                at = r["bar_start"]
                rows.append(
                    RejectRow(
                        r["instrument_id"],
                        r["symbol"],
                        r["day"],
                        None if at is None else as_utc(at),
                        r["rule"],
                    )
                )
        return rows

    def read_coverage(self, day: date) -> Coverage:
        """The stored coverage of `day`; none (nothing watched from the open) when absent or
        unreadable, so a restored opening range fails closed."""
        path = self.partition(day) / COVERAGE_NAME
        if not path.is_file():
            return Coverage()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            gap, seen = raw.get("gap_from"), raw.get("seen_until")
            return Coverage(
                frozenset(str(i) for i in raw.get("from_open", ())),
                None if gap is None else as_utc(datetime.fromisoformat(gap)),
                None if seen is None else as_utc(datetime.fromisoformat(seen)),
            )
        except (OSError, ValueError, TypeError, AttributeError):
            return Coverage()

    def write_coverage(self, day: date, coverage: Coverage) -> None:
        """Merge `coverage` into the stored one: the union of the instruments, the earlier
        gap, the later seen instant. Written whole through a temporary file and an atomic
        replace."""
        stored = self.read_coverage(day)
        gaps = [g for g in (stored.gap_from, coverage.gap_from) if g is not None]
        seen = [s for s in (stored.seen_until, coverage.seen_until) if s is not None]
        merged = Coverage(
            stored.from_open | coverage.from_open, min(gaps, default=None), max(seen, default=None)
        )
        if merged == stored and self.partition(day).joinpath(COVERAGE_NAME).is_file():
            return
        body = {
            "from_open": sorted(merged.from_open),
            "gap_from": None if merged.gap_from is None else as_utc(merged.gap_from).isoformat(),
            "seen_until": None
            if merged.seen_until is None
            else as_utc(merged.seen_until).isoformat(),
        }
        self._atomic(self.partition(day) / COVERAGE_NAME, json.dumps(body).encode())

    def _atomic(self, target: Path, data: bytes) -> None:
        atomic_replace(target, lambda tmp: tmp.write_bytes(data))

    def _replace(self, day: date, rest: pa.Table, rows: list[BarRow5m]) -> None:
        """Write `rest` (stored rows kept as they are) and `rows` as the day's file."""
        fresh = pa.table(
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
        table = pa.concat_tables([rest, fresh]).sort_by(
            [("bar_start", "ascending"), ("instrument_id", "ascending")]
        )
        self._atomic_table(self.path(day), table)

    def _atomic_table(self, target: Path, table: pa.Table) -> None:
        atomic_replace(target, lambda tmp: pq.write_table(table, tmp))


def _rows(table: pa.Table) -> list[BarRow5m]:
    return [
        BarRow5m(
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
