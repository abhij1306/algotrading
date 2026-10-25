"""The history build's validation (HAT-DATA-096): row counts per day, and a seeded random
sample of symbol-days whose close is compared with the MCP's `get_stock_history`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..money import exact
from .mcp_client import McpClient, McpError, parse_data_date
from .store import LATEST_ROW, DataStore

SAMPLE_SIZE = 50


CloseSource = Callable[[str, date], Decimal | None]


def mcp_close_source(client: McpClient) -> CloseSource:
    """The close `get_stock_history` gives for a symbol on a day (raw, unadjusted)."""

    def close(symbol: str, day: date) -> Decimal | None:
        try:
            rows = client.stock_history(symbol, day, 1)
        except McpError:
            return None
        for row in rows:
            if parse_data_date(row.get("date")) == day:
                return exact(row.get("close"), floats=True, grouped=True)
        return None

    return close


@dataclass(frozen=True)
class SampleCheck:
    symbol: str
    day: date
    ours: Decimal
    theirs: Decimal | None

    @property
    def mismatch(self) -> bool:
        return self.theirs is not None and self.theirs != self.ours


@dataclass
class ValidationReport:
    row_counts: dict[date, int] = field(default_factory=dict)
    samples: list[SampleCheck] = field(default_factory=list)

    @property
    def mismatches(self) -> list[SampleCheck]:
        return [s for s in self.samples if s.mismatch]

    @property
    def unchecked(self) -> list[SampleCheck]:
        return [s for s in self.samples if s.theirs is None]

    @property
    def failed(self) -> bool:
        """True iff some sampled close differs from the MCP close (exact comparison)."""
        return bool(self.mismatches)


_SAMPLE_SQL = f"""
SELECT symbol, date, close FROM (
    SELECT instrument_id, symbol, date, close, ingest_run_id,
        sha256(concat(?, ':', instrument_id, ':', CAST(date AS VARCHAR))) AS h
    FROM bars_daily_raw ORDER BY h, ingest_run_id DESC LIMIT ?)
{LATEST_ROW}
ORDER BY h LIMIT ?
"""  # noqa: S608


def validate_build(
    store: DataStore,
    close_source: CloseSource,
    *,
    sample_size: int = SAMPLE_SIZE,
    seed: int = 0,
) -> ValidationReport:
    """Row counts per day, and `sample_size` random symbol-days compared with the MCP.

    The sample is the instrument-days with the smallest sha256 of `seed:instrument_id:day`, drawn
    in DuckDB so only the sample reaches Python. Every run's row of one instrument-day has the
    same hash, so the first `sample_size x runs` rows by (hash, newest run) hold at least
    `sample_size` instrument-days, each with its latest row: no window over the whole table."""
    report = ValidationReport()
    con = store.connect()
    try:
        report.row_counts = dict(
            con.execute(
                "SELECT date, count(DISTINCT CAST(instrument_id AS UUID)) FROM bars_daily_raw "
                "GROUP BY date ORDER BY date"
            ).fetchall()
        )
        runs = con.execute("SELECT count(DISTINCT ingest_run_id) FROM bars_daily_raw").fetchone()
        pool = sample_size * max(runs[0] if runs else 0, 1)
        chosen = con.execute(_SAMPLE_SQL, [str(seed), pool, sample_size]).fetchall()
    finally:
        con.close()
    for symbol, day, ours in sorted(chosen, key=lambda r: (r[1], r[0])):
        report.samples.append(SampleCheck(symbol, day, ours, close_source(symbol, day)))
    return report


@dataclass(frozen=True)
class Validation:
    """The MCP close lookup and the size and seed of the sample compared with it."""

    close_source: CloseSource
    sample_size: int = SAMPLE_SIZE
    seed: int = 0
