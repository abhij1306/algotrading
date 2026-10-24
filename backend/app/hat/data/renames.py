"""Symbol renames rebuilt from NSE data (HAT-DATA-049, owner decision D68).

HAT reads no legacy table. A bhavcopy row names its security by ISIN and series and gives the
symbol it traded under that day, so a symbol that changes for one (ISIN, series) between two days
in `bars_daily_raw` is a rename, effective on the first day of the new symbol. The records go to
`instruments.seed_symbol_history` (as the `renames` source of `reference.weekly`), which places
them in `hat_instrument_symbol_history`, and to the history build (D1). The instrument master
places a rename on the day it first lists the new symbol; the bars give the day it took effect.

    python -m app.hat.data.renames

seeds the symbol history from the bars under `HAT_DATA_ROOT` now, in one transaction, and prints
how many renames it read and how many matched no instrument.
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy import select

from .instruments import SymbolRename, seed_symbol_history
from .store import DataStore

_RENAMES_SQL = """
WITH seen AS (
    SELECT DISTINCT isin, series, date, symbol FROM bars_daily_raw
),
ordered AS (
    SELECT isin, series, date, symbol,
           lag(symbol) OVER (PARTITION BY isin, series ORDER BY date) AS previous
    FROM seen
)
SELECT isin, previous, symbol, date FROM ordered
WHERE previous IS NOT NULL AND previous <> symbol
ORDER BY date, isin, series
"""


def renames_from_bars(store: DataStore) -> list[SymbolRename]:
    """Every symbol change in the stored raw bars, oldest first."""
    con = store.connect()
    try:
        rows = con.execute(_RENAMES_SQL).fetchall()
    finally:
        con.close()
    return [SymbolRename(old, new, day, isin) for isin, old, new, day in rows]


def symbol_first_bars(store: DataStore) -> dict[tuple[uuid.UUID, str], date]:
    """Each (instrument, symbol)'s first raw bar: who took a symbol when, for the symbol
    history repair (`instruments.repair_symbol_history`)."""
    con = store.connect()
    try:
        rows = con.execute(
            "SELECT instrument_id, symbol, min(date) FROM bars_daily_raw GROUP BY 1, 2"
        ).fetchall()
    finally:
        con.close()
    return {(uuid.UUID(i), symbol): day for i, symbol, day in rows if i}


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    if argv:
        sys.stderr.write("usage: python -m app.hat.data.renames\n")
        return 2
    from ..settings import operator_settings

    settings = operator_settings()  # before the database module, which reads .env too
    from app.database import HatSessionLocal

    from ..models import HatMarket

    renames = renames_from_bars(DataStore(settings.data_root, memory_limit=settings.duckdb_memory))
    with HatSessionLocal() as session:
        market_id = session.scalar(select(HatMarket.id).where(HatMarket.code == "NSE"))
        if market_id is None:
            sys.stderr.write("refused: no_market\n")
            return 2
        unmatched = seed_symbol_history(session, market_id, renames)
        session.commit()
    sys.stdout.write(json.dumps({"renames": len(renames), "unmatched": unmatched}) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
