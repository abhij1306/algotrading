"""The data snapshot a verify report judged: one digest of the verified stores' files and the
reference tables' rows in the report's window, so a report verifies a backtest only while the
data it judged is unchanged (`status_for` recomputes it and compares).

- Files: every file of the dated stores (`bars_daily_raw`, `bars_5m`, `index_daily`: the
  `date=` partition) and every bhavcopy and index file under `raw/` (the date in its name) whose
  day is in the window, and each `bars_5m_rejects` part whose date range meets it, by content:
  the sha256 the data record's manifest holds for the file when the file has not changed since
  the manifest was written (its status-change time, which every write moves and no tool can set
  back, is older than the manifest), else the file's own sha256 (`file_entry`). Each ETF store
  file's rows in the window by content (the files are appended whole every day), and the Fyers
  download windows (`minute_history/progress.json`) cut to the window.
- Tables: the rows each check reads, in the window, hashed in the database (`md5` of the rows'
  text in a fixed order), with open spans cut at the window's end so a later close does not
  change the past. Instruments: the ones listed by the window's end, their identity and listing
  dates (not today's `active` flag: the checks that read it only warn).

New days after the window, and rows dated after it, leave the digest unchanged; anything in the
window that changes (a rewritten partition, a corrected factor, a new filing time) changes it.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

DATED_STORES = ("bars_daily_raw", "bars_5m", "index_daily")
RANGED_STORES = ("bars_5m_rejects",)  # undated parts: by the date range of their rows
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
    "corporate_action_deletions": (
        "select t.instrument_id, t.ex_date, t.action_type, t.status "
        "from hat_corporate_action_version t "
        "where t.status = 'deleted' and t.ex_date between :start and :end"
    ),
    "index_membership": (
        "select t.index_name, t.instrument_id, t.symbol, greatest(t.effective_from, :start), "
        "least(coalesce(t.effective_to, :end), :end) from hat_index_member t "
        "where t.effective_from <= :end and coalesce(t.effective_to, :end) >= :start"
    ),
    "index_membership_cover": (
        "select t.index_name, greatest(t.covered_from, :start), least(t.covered_to, :end) "
        "from hat_index_membership t where t.covered_from <= :end and t.covered_to >= :start"
    ),
    "instruments": (
        "select t.id, t.isin, t.symbol, t.series, t.listed_on, "
        "case when t.delisted_on <= :end then t.delisted_on end "
        "from hat_instrument t where t.listed_on is null or t.listed_on <= :end"
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


def files_digest(entries: Iterable[tuple[str, str]]) -> str:
    """The sha256 of (path, content sha256) entries, in path order."""
    text = "\n".join(f"{p}|{h}" for p, h in sorted(entries))
    return hashlib.sha256(text.encode()).hexdigest()


def file_entry(
    size: int,
    changed_ns: int,
    recorded: Mapping[str, str] | None,
    recorded_ns: int | None,
    content: Callable[[], str],
) -> str:
    """A file's content sha256: the manifest's (`recorded`, its row) when the file is the size
    it recorded and has not changed since the manifest was written (`changed_ns`, the file's
    status-change time, before `recorded_ns`), else read from the file (`content`)."""
    if (
        recorded is not None
        and recorded_ns is not None
        and changed_ns < recorded_ns
        and recorded.get("bytes") == str(size)
        and recorded.get("sha256")
    ):
        return recorded["sha256"]
    return content()


def _manifest(manifests: Path | None, store: str) -> tuple[dict[str, dict[str, str]], int | None]:
    path = None if manifests is None else manifests / f"{store}.csv"
    if path is None or not path.is_file():
        return {}, None
    with path.open(newline="", encoding="utf-8") as handle:
        rows = {row["path"]: row for row in csv.DictReader(handle)}
    return rows, path.stat().st_mtime_ns


def _in_window(path: Path, rel: str, store: str, start: date, end: date) -> bool:
    from ..record import parquet_facts  # noqa: PLC0415

    if store in RANGED_STORES:
        if path.suffix != ".parquet":
            return True
        _, low, high = parquet_facts(path)
        return low is None or high is None or (low <= end and start <= high)
    day = file_day(rel)
    return day is not None and start <= day <= end


def _store_files(
    root: Path, start: date, end: date, manifests: Path | None
) -> dict[str, list[tuple[str, str]]]:
    from ..record import file_sha256  # noqa: PLC0415

    found: dict[str, list[tuple[str, str]]] = {}
    for store in (*DATED_STORES, *RANGED_STORES, "raw"):
        base = root / store
        if store == "raw":
            paths = base.glob("*")
        elif store in RANGED_STORES:
            paths = base.rglob("*")
        else:
            paths = base.glob("date=*/*")
        recorded, recorded_ns = _manifest(manifests, store)
        entries = []
        for path in paths:
            rel = path.relative_to(root).as_posix()
            if path.name.startswith(".") or not path.is_file():
                continue
            if not _in_window(path, rel, store, start, end):
                continue
            info = path.stat()
            entries.append(
                (
                    rel,
                    file_entry(
                        info.st_size,
                        info.st_ctime_ns,
                        recorded.get(rel),
                        recorded_ns,
                        lambda p=path: file_sha256(p),
                    ),
                )
            )
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


def windows_digest(windows: Mapping[str, tuple[date, date]], start: date, end: date) -> str:
    """The sha256 of the download windows (symbol -> first, last day) cut to `start..end`; a
    window outside it adds nothing."""
    cut = sorted(
        f"{s}|{max(a, start)}|{min(b, end)}"
        for s, (a, b) in windows.items()
        if a <= end and start <= b
    )
    return hashlib.sha256("\n".join(cut).encode()).hexdigest()


def _fyers_digest(root: Path, start: date, end: date) -> str:
    from .bars5m import fyers_windows  # noqa: PLC0415

    try:
        progress = json.loads((root / "minute_history" / "progress.json").read_text())
    except (OSError, ValueError):
        return "absent"
    return windows_digest(fyers_windows(progress), start, end)


def snapshot(
    root: Path,
    session: Any,  # noqa: ANN401 - a SQLAlchemy session
    start: date,
    end: date,
    manifests: Path | None = None,
) -> dict[str, Any]:
    """The window's data snapshot: the digest and its parts (one per store and table).
    `manifests` is the data record's manifest folder (its sha256s spare reading unchanged
    files); by default the operator's data repo's."""
    from sqlalchemy import text  # noqa: PLC0415

    from ...models import HatMarket  # noqa: PLC0415

    if manifests is None:
        from ...settings import operator_settings  # noqa: PLC0415
        from ..record import MANIFESTS  # noqa: PLC0415

        manifests = operator_settings().data_repo / MANIFESTS
    parts = {
        f"files:{k}": files_digest(v) for k, v in _store_files(root, start, end, manifests).items()
    }
    for name in ETF_FILES:
        parts[f"etf:{name}"] = _etf_digest(root / "etf" / name, start, end)
    parts["fyers_windows"] = _fyers_digest(root, start, end)
    for name, sql in TABLES.items():
        query = (
            f"select md5(coalesce(string_agg(x::text, E'\\n' order by x::text), '')) from ({sql}) x"  # noqa: S608, E501 - fixed queries
        )
        value = session.execute(
            text(query), {"start": start, "end": end}, bind_arguments={"mapper": HatMarket}
        ).scalar()
        parts[f"table:{name}"] = str(value)
    return {"digest": digest(parts), "parts": parts}
