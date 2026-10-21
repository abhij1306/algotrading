"""Symbol renames rebuilt from NSE data (HAT-DATA-049, owner decision D68).

HAT reads no legacy table. A bhavcopy row names its security by ISIN and series and gives the
symbol it traded under that day, so a symbol that changes for one (ISIN, series) between two days
in `bars_daily_raw` is a rename, effective on the first day of the new symbol. The records go to
`instruments.seed_symbol_history` (as the `renames` source of `reference.weekly`), which places
them in `hat_instrument_symbol_history`.
"""

from __future__ import annotations

from .instruments import SymbolRename
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
