"""NSE's symbol-change and name-change lists (`symbolchange.csv`, `namechange.csv`), which the
lineage rules read to tell a renamed security from a new one (PR 74)."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .chains import LineageError

SYMBOL_CHANGES_URL = "https://nsearchives.nseindia.com/content/equities/symbolchange.csv"


NAME_CHANGES_URL = "https://nsearchives.nseindia.com/content/equities/namechange.csv"


@dataclass(frozen=True)
class SymbolChange:
    old: str
    new: str
    effective: date


@dataclass(frozen=True)
class NameChange:
    symbol: str
    old_name: str
    new_name: str
    effective: date


def _nse_date(raw: str) -> date | None:
    try:
        return datetime.strptime(raw.strip(), "%d-%b-%Y").date()
    except ValueError:
        return None


def _csv_rows(text: str) -> list[dict[str, str]]:
    return [
        {(k or "").strip().upper(): (v or "").strip() for k, v in row.items()}
        for row in csv.DictReader(io.StringIO(text))
    ]


def parse_symbol_changes(text: str) -> list[SymbolChange]:
    """NSE's `symbolchange.csv` (NAME OF THE COMPANY, OLD SYMBOL, NEW SYMBOL, EFFECTIVE DATE);
    a row without both symbols or a readable date is skipped."""
    out = []
    for row in _csv_rows(text):
        old, new = row.get("OLD SYMBOL", "").upper(), row.get("NEW SYMBOL", "").upper()
        day = _nse_date(row.get("EFFECTIVE DATE", ""))
        if old and new and old != new and day is not None:
            out.append(SymbolChange(old, new, day))
    return out


def parse_name_changes(text: str) -> list[NameChange]:
    """NSE's `namechange.csv` (SYMBOL, OLD NAME OF THE COMPANY, NEW NAME OF THE COMPANY,
    EFFECTIVE DATE)."""
    out = []
    for row in _csv_rows(text):
        symbol = row.get("SYMBOL", "").upper()
        day = _nse_date(row.get("EFFECTIVE DATE", ""))
        if symbol and day is not None:
            out.append(
                NameChange(
                    symbol,
                    row.get("OLD NAME OF THE COMPANY", ""),
                    row.get("NEW NAME OF THE COMPANY", ""),
                    day,
                )
            )
    return out


def read_list(path: str | None, url: str) -> str:  # pragma: no cover - a file or the network
    if path is not None:
        # A CSV under the working directory: anything resolving outside it, or with another
        # suffix, is refused before it is opened.
        base = Path.cwd().resolve()
        target = (base / path).resolve()
        if not target.is_relative_to(base) or target.suffix.lower() != ".csv":
            raise LineageError("lineage_file_outside_cwd")
        return target.read_text("utf-8-sig")
    from .bhavcopy import http_fetcher

    result = http_fetcher()(url)
    if result.status != "ok":
        raise LineageError("lineage_download_failed")
    return result.data.decode("utf-8-sig")
