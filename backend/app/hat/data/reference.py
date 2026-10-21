"""Source-side reference data: the equity listing, NSE list parsers, price bands, industries
(HAT-DATA-023, 025, 026). No database here; `instruments.py` writes the results.

The cm-market MCP lists symbol and series but no ISIN, name or face value, so the NSE equity
list CSV (`EQUITY_L.csv`) supplies those and the MCP listing decides which securities exist
this week. A listed symbol with no CSV row is reported as unresolved, never given an invented
ISIN.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from .mcp_client import McpClient, McpError

EQUITY_SERIES = frozenset({"EQ", "BE", "BZ", "BL", "BT", "IL", "IQ"})
PAGE_CAP = 500  # cm_get_equity_stocks returns at most 500 rows per call
PREFIX_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789&-"
MAX_PREFIX_DEPTH = 10
CODE_LISTING_TRUNCATED = "mcp_listing_truncated"
BAND_VALUES = (2, 5, 10, 20)
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
_MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()


@dataclass(frozen=True)
class Security:
    """One listed security: the instrument key is (isin, series)."""

    isin: str
    series: str
    symbol: str
    name: str
    face_value: Decimal
    listed_on: date | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.isin, self.series)


@dataclass(frozen=True)
class ListedSymbol:
    symbol: str
    series: str


@dataclass(frozen=True)
class ListingBuild:
    securities: list[Security]
    unresolved: list[ListedSymbol] = field(default_factory=list)  # listed, but no ISIN row
    source: str = "mcp"


def _keep_page(
    found: dict[tuple[str, str], dict[str, object]], prefix: str, rows: list[dict[str, object]]
) -> None:
    for row in rows:
        symbol = str(row["symbol"]).strip().upper()
        if symbol.startswith(prefix):
            series = str(row.get("series") or "").strip().upper()
            found[(symbol, series)] = row


def page_equity_rows(client: McpClient, *, cap: int = PAGE_CAP) -> dict[tuple[str, str], dict]:
    """Every equity row the listing returns, keyed by (symbol, series), prefix-paged: a page that
    reaches `cap` is never accepted as complete; its prefix is split by one more character until
    each page is under the cap. The 09:16 gap scan reads the prices off the same rows.

    The rows of a capped page are kept too, so a symbol equal to the prefix itself (which a
    deeper prefix cannot return) is not lost when the server lists it first.
    """
    found: dict[tuple[str, str], dict[str, object]] = {}
    level = list(PREFIX_ALPHABET)
    depth = 1
    while level:
        pages = client.map_calls([(lambda p=p: client.equity_stocks(p, cap)) for p in level])
        capped = [p for p, rows in zip(level, pages, strict=True) if len(rows) >= cap]
        for prefix, rows in zip(level, pages, strict=True):
            _keep_page(found, prefix, rows)
        if capped and depth >= MAX_PREFIX_DEPTH:
            raise McpError(CODE_LISTING_TRUNCATED, attempts=0)
        level = [prefix + c for prefix in capped for c in PREFIX_ALPHABET]
        depth += 1
    return found


def page_listing(client: McpClient, *, cap: int = PAGE_CAP) -> list[ListedSymbol]:
    """Every equity symbol and series, prefix-paged (see `page_equity_rows`)."""
    found = page_equity_rows(client, cap=cap)
    return [ListedSymbol(symbol, series) for symbol, series in sorted(found)]


def _parse_listing_date(raw: str) -> date | None:
    parts = raw.strip().upper().split("-")
    if len(parts) != 3 or parts[1] not in _MONTHS:
        return None
    try:
        return date(int(parts[2]), _MONTHS.index(parts[1]) + 1, int(parts[0]))
    except ValueError:
        return None


def _rows(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    out = []
    for raw in reader:
        out.append({(k or "").strip().upper(): (v or "").strip() for k, v in raw.items()})
    return out


def parse_equity_list_csv(text: str) -> list[Security]:
    """Parse NSE's equity list CSV; rows with a bad ISIN, symbol or series are skipped."""
    securities: dict[tuple[str, str], Security] = {}
    for row in _rows(text):
        symbol = row.get("SYMBOL", "").upper()
        series = row.get("SERIES", "").upper()
        isin = row.get("ISIN NUMBER", row.get("ISIN", "")).upper()
        if not symbol or series not in EQUITY_SERIES or not ISIN_RE.match(isin):
            continue
        try:
            face_value = Decimal(row.get("FACE VALUE", "") or "0")
        except InvalidOperation:
            face_value = Decimal(0)
        security = Security(
            isin=isin,
            series=series,
            symbol=symbol,
            name=row.get("NAME OF COMPANY", symbol) or symbol,
            face_value=face_value,
            listed_on=_parse_listing_date(row.get("DATE OF LISTING", "")),
        )
        securities[security.key] = security
    return list(securities.values())


def build_listing_from_mcp(
    listed: Iterable[ListedSymbol], csv_rows: list[Security]
) -> ListingBuild:
    """The securities the MCP lists, with ISIN data from the CSV; the rest are unresolved."""
    by_symbol = {(s.symbol, s.series): s for s in csv_rows}
    securities: dict[tuple[str, str], Security] = {}
    unresolved = []
    for item in listed:
        match = by_symbol.get((item.symbol, item.series))
        if match is None:
            unresolved.append(item)
        else:
            securities[match.key] = match
    return ListingBuild(list(securities.values()), unresolved, "mcp")


def build_listing_from_csv(csv_rows: list[Security]) -> ListingBuild:
    """The fallback when the MCP listing fails: the CSV alone (HAT-DATA-023)."""
    return ListingBuild(list({s.key: s for s in csv_rows}.values()), [], "csv")


# --- price bands (HAT-DATA-025) --------------------------------------------------------------


@dataclass(frozen=True)
class BandRow:
    symbol: str
    series: str
    band_pct: Decimal | None  # None = no band


def parse_band_value(raw: str) -> tuple[bool, Decimal | None]:
    """(recognised, band). "No Band" and similar mean no band; anything unknown is rejected."""
    value = raw.strip().upper().rstrip("%").strip()
    if value in {"NO BAND", "NONE", "NIL", "NA", "-", ""}:
        return True, None
    try:
        pct = Decimal(value)
    except InvalidOperation:
        return False, None
    return (True, pct) if pct in {Decimal(b) for b in BAND_VALUES} else (False, None)


def parse_price_band_csv(text: str) -> tuple[list[BandRow], int]:
    """Rows of NSE's price-band file and the count of rows rejected for an unknown band.

    [A] The real file's location and columns are unverified (HAT-DATA-025); this parser accepts
    columns `Symbol`, `Series` and `Band` (or `Price Band`) and is validated on the first live
    download.
    """
    rows: dict[tuple[str, str], BandRow] = {}
    rejected = 0
    for row in _rows(text):
        symbol = row.get("SYMBOL", "").upper()
        series = row.get("SERIES", "").upper()
        raw = row.get("BAND", row.get("PRICE BAND", ""))
        if not symbol or not series:
            continue
        ok, pct = parse_band_value(raw)
        if not ok:
            rejected += 1
            continue
        rows[(symbol, series)] = BandRow(symbol, series, pct)
    return list(rows.values()), rejected


def band_from_circuit_limits(
    prev_close: Decimal, upper: Decimal, lower: Decimal, tolerance: Decimal = Decimal("0.6")
) -> Decimal | None:
    """The fallback: a band from a broker quote's circuit limits, or None when the limits do not
    match one of 2/5/10/20 % of the previous close (undetermined, so nothing is stored)."""
    if prev_close <= 0 or upper <= prev_close or lower >= prev_close:
        return None
    up = (upper - prev_close) / prev_close * 100
    down = (prev_close - lower) / prev_close * 100
    for band in BAND_VALUES:
        target = Decimal(band)
        if abs(up - target) <= tolerance and abs(down - target) <= tolerance:
            return target
    return None


# --- industry (HAT-DATA-026) ------------------------------------------------------------------


@dataclass(frozen=True)
class IndustryRow:
    symbol: str
    isin: str
    sector: str
    industry: str


def parse_industry_csv(text: str) -> list[IndustryRow]:
    """NSE index-constituent CSV (`Company Name, Industry, Symbol, Series, ISIN Code`).

    [A] Only an `Industry` column exists there, so the sector defaults to it unless a `Sector`
    column is present; validated on the first live download.
    """
    rows: dict[str, IndustryRow] = {}
    for row in _rows(text):
        symbol = row.get("SYMBOL", "").upper()
        industry = row.get("INDUSTRY", "")
        if not symbol or not industry:
            continue
        isin = row.get("ISIN CODE", row.get("ISIN", "")).upper()
        rows[symbol] = IndustryRow(symbol, isin, row.get("SECTOR") or industry, industry)
    return list(rows.values())
