"""The data snapshot a verify report judged: one digest of the verified stores' files and the
reference tables' rows in the report's window, so a report verifies a backtest only while the
data it judged is unchanged (`status_for` recomputes it and compares).

- Files: every file of the dated stores (`bars_daily_raw`, `bars_5m`, `index_daily`: the
  `date=` partition) and every bhavcopy and index file under `raw/` (the date in its name) whose
  day is in the window, by path, size and modification time (a rewrite changes both; the record
  manifests' sha256, `app.hat.data.record`, is the content history); each ETF store file's rows
  in the window by content (the files are appended whole every day).
- Tables: the rows each check reads, in the window, hashed in the database (`md5` of the rows'
  text in a fixed order), with open spans cut at the window's end so a later close does not
  change the past.

New days after the window, and rows dated after it, leave the digest unchanged; anything in the
window that changes (a rewritten partition, a corrected factor, a new filing time) changes it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

DATED_STORES = ("bars_daily_raw", "bars_5m", "index_daily")
ETF_FILES = ("daily.parquet", "indices.parquet", "liquid_tr.parquet", "bars_5m.parquet")
_PARTITION = re.compile(r"^date=(\d{4}-\d{2}-\d{2})$")
_RAW_NAMES = (
    (re.compile(r"^cm(\d{2}[A-Z]{3}\d{4})bhav\.csv\.zip$"), "%d%b%Y"),
    (re.compile(r"^ind_close_all_(\d{8})\.csv$"), "%d%m%Y"),
    (re.compile(r"^BhavCopy_NSE_CM_0_0_0_(\d{8})_F_0000\.csv\.zip$"), "%Y%m%d"),
)

# Each table's rows a check judges in `:start..:end` (open spans cut at :end).
TABLES: dict[str, str] = {
    "corporate_actions": (
        "select t.instrument_id, t.ex_date, t.action_type, t.adjustment_factor, t.cash_amount, "
        "t.demerger, t.source, t.raw_text from hat_corporate_action t "
        "where t.ex_date between :start and :end"
    ),
    "index_membership": (
        "select t.index_name, t.instrument_id, t.symbol, greatest(t.effective_from, :start), "
        "least(coalesce(t.effective_to, :end), :end) from hat_index_member t "
        "where t.effective_from <= :end and coalesce(t.effective_to, :end) >= :start"
    ),
    "results_filings": (
        "select t.symbol, t.isin, t.period_end, t.source, t.public_at, t.public_at_source "
        "from hat_financial_result t where t.public_at::date between :start and :end"
    ),
    "trading_calendar": (
        "select t.market_id, t.date, t.is_trading from hat_trading_calendar t "
        "where t.date between :start and :end"
    ),
    "symbol_history": (
        "select t.instrument_id, t.symbol, greatest(t.valid_from, :start), "
        "least(coalesce(t.valid_to, :end), :end) from hat_instrument_symbol_history t "
        "where t.valid_from <= :end and coalesce(t.valid_to, :end) >= :start"
    ),
    "isin_lineage": (
        "select t.old_instrument_id, t.new_instrument_id, t.effective_date, t.status "
        "from hat_isin_lineage t where t.effective_date between :start and :end"
    ),
}


def file_day(rel: str) -> date | None:
    """The day a store file holds: its `date=` partition, or the date in a `raw/` bhavcopy or
    index file's name; None for any other file."""
    parts = rel.split("/")
    for part in parts[:-1]:
        if match := _PARTITION.match(part):
            return date.fromisoformat(match.group(1))
    if len(parts) == 2 and parts[0] == "raw":  # noqa: PLR2004 - raw/<file>, not its subfolders
        for pattern, fmt in _RAW_NAMES:
            if match := pattern.match(parts[1]):
                return datetime.strptime(match.group(1).title(), fmt).date()  # noqa: DTZ007
    return None


def digest(parts: Mapping[str, str]) -> str:
    """One sha256 of the named part digests."""
    text = "\n".join(f"{k}={parts[k]}" for k in sorted(parts))
    return hashlib.sha256(text.encode()).hexdigest()


def files_digest(entries: Iterable[tuple[str, int, int]]) -> str:
    """The sha256 of (path, size, mtime_ns) entries, in path order."""
    text = "\n".join(f"{p}|{s}|{m}" for p, s, m in sorted(entries))
    return hashlib.sha256(text.encode()).hexdigest()


def _store_files(root: Path, start: date, end: date) -> dict[str, list[tuple[str, int, int]]]:
    found: dict[str, list[tuple[str, int, int]]] = {}
    for store in (*DATED_STORES, "raw"):
        base = root / store
        paths = base.glob("*") if store == "raw" else base.glob("date=*/*")
        entries = []
        for path in paths:
            rel = path.relative_to(root).as_posix()
            day = file_day(rel)
            if day is None or not start <= day <= end or not path.is_file():
                continue
            info = path.stat()
            entries.append((rel, info.st_size, info.st_mtime_ns))
        found[store] = entries
    return found


def _etf_digest(path: Path, start: date, end: date) -> str:
    import pyarrow.compute as pc  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415

    if not path.is_file():
        return "absent"
    table = pq.read_table(path)
    column = "date" if "date" in table.column_names else "start"
    days = table[column] if column == "date" else pc.cast(table[column], "date32")
    keep = pc.and_(pc.greater_equal(days, start), pc.less_equal(days, end))
    rows = table.filter(keep).to_pylist()
    lines = sorted("|".join(f"{k}={row[k]}" for k in sorted(row)) for row in rows)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def snapshot(root: Path, session: Any, start: date, end: date) -> dict[str, Any]:  # noqa: ANN401
    """The window's data snapshot: the digest and its parts (one per store and table)."""
    from sqlalchemy import text  # noqa: PLC0415

    from ...models import HatMarket  # noqa: PLC0415

    parts = {f"files:{k}": files_digest(v) for k, v in _store_files(root, start, end).items()}
    for name in ETF_FILES:
        parts[f"etf:{name}"] = _etf_digest(root / "etf" / name, start, end)
    for name, sql in TABLES.items():
        query = (
            f"select md5(coalesce(string_agg(x::text, E'\\n' order by x::text), '')) from ({sql}) x"  # noqa: S608, E501 - fixed queries
        )
        value = session.execute(
            text(query), {"start": start, "end": end}, bind_arguments={"mapper": HatMarket}
        ).scalar()
        parts[f"table:{name}"] = str(value)
    return {"digest": digest(parts), "parts": parts}
