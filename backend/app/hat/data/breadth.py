"""The day's advance/decline record: the MCP's `get_market_breadth` when it answers for the
day, else counted from our own bars, written to `market_breadth` by the nightly ingest."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal
from typing import Any

import pyarrow as pa

from .store import DATASET_MARKET_BREADTH, MARKET_BREADTH_SCHEMA, DataStore

SOURCE_MCP = "mcp"
SOURCE_DERIVED = "derived"  # breadth counted from our own bars when the MCP has none
BREADTH_PART = "breadth"


def derive_breadth(frame_rows: Iterable[tuple[Decimal | None, Decimal, int]]) -> dict[str, Any]:
    """Advances, declines and unchanged counted from (prev_close, close, volume) rows."""
    advances = declines = unchanged = volume = 0
    for prev, close, vol in frame_rows:
        volume += vol
        if prev is None or close == prev:
            unchanged += 1
        elif close > prev:
            advances += 1
        else:
            declines += 1
    total = advances + declines + unchanged
    return {
        "total_stocks": total,
        "advances": advances,
        "declines": declines,
        "unchanged": unchanged,
        "ad_ratio": (advances / declines) if declines else None,
        "volume": volume,
        "source": SOURCE_DERIVED,
    }


def _count(fields: Mapping[str, object], *names: str) -> int | None:
    for name in names:
        value = fields.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def breadth_from_mcp(fields: Mapping[str, object]) -> dict[str, Any] | None:
    """The `get_market_breadth` fields as a row, or None when a count is missing."""
    advances = _count(fields, "advances")
    declines = _count(fields, "declines")
    unchanged = _count(fields, "unchanged")
    if advances is None or declines is None or unchanged is None:
        return None
    total = _count(fields, "totalStocks", "total_stocks", "total")
    ratio = fields.get("adRatio", fields.get("ad_ratio"))
    if isinstance(ratio, int | float) and not isinstance(ratio, bool):
        ad_ratio: float | None = float(ratio)
    else:
        ad_ratio = advances / declines if declines else None
    return {
        "total_stocks": total if total is not None else advances + declines + unchanged,
        "advances": advances,
        "declines": declines,
        "unchanged": unchanged,
        "ad_ratio": ad_ratio,
        "volume": _count(fields, "total_volume", "totalVolume", "volume") or 0,
        "source": SOURCE_MCP,
    }


def write_breadth(store: DataStore, day: date, row: Mapping[str, Any]) -> None:
    table = pa.table(
        {k: [row[k]] for k in MARKET_BREADTH_SCHEMA.names}, schema=MARKET_BREADTH_SCHEMA
    )
    store.write_partition(DATASET_MARKET_BREADTH, day, BREADTH_PART, table)
