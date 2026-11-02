"""Fyers' public symbol master of NSE's cash market (`NSE_CM.csv`): each listed symbol's series
and ISIN, the names `mcp_bars` reads the bhavcopy MCP's rows by. Fyers is the broker; the file
needs no login."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping

from .reference import EQUITY_SERIES, ISIN_RE

FYERS_NSE_CM_URL = "https://public.fyers.in/sym_details/NSE_CM.csv"

# (read 2026-11-02).
_FY_ISIN, _FY_TICKER = 5, 9


def fyers_listings(text: str) -> dict[tuple[str, str], str]:
    """(symbol, series) -> ISIN of every equity-series row of Fyers' `NSE_CM.csv`; a row with an
    unreadable ticker or ISIN is left out."""
    out: dict[tuple[str, str], str] = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) <= _FY_TICKER:
            continue
        ticker, isin = row[_FY_TICKER].strip().upper(), row[_FY_ISIN].strip().upper()
        if not ticker.startswith("NSE:") or "-" not in ticker or not ISIN_RE.match(isin):
            continue
        symbol, series = ticker[4:].rsplit("-", 1)
        if symbol and series in EQUITY_SERIES:
            out[(symbol, series)] = isin
    return out


def download_nse_cm() -> str:  # pragma: no cover - the network
    import requests

    from .nse_web import guard

    guard(FYERS_NSE_CM_URL)
    response = requests.get(FYERS_NSE_CM_URL, timeout=120)
    response.raise_for_status()
    return response.content.decode("utf-8-sig", errors="replace")


# [A] Fyers' master lists every traded equity (2,993 on 2026-11-02): a master naming fewer of the
# last session's ISINs is a changed or broken file, not the market.
MIN_SHARE = 0.9


def master_refused(listed: Mapping[tuple[str, str], str], stored: Iterable[str]) -> str | None:
    """Why `listed` (`fyers_listings`) cannot be the master, else None: no listing at all, or
    fewer than `MIN_SHARE` of the ISINs of the last stored session (`stored`). Harm: the
    symbols it drops never asked of the MCP, a new listing's bars silently missing."""
    if not listed:
        return "fyers_master_unreadable: no equity listing"
    want = set(stored)
    have = want & set(listed.values())
    if want and len(have) < MIN_SHARE * len(want):
        return f"fyers_master_short: {len(have)} of the last session's {len(want)} ISINs"
    return None
