"""Kite (Zerodha) trading symbols of HAT's instruments, as pure functions (HAT-DATA-024).

Kite's instrument dump names each NSE stock by its trading symbol, Kite's numeric
`instrument_token` and NSE's `exchange_token`, but carries no ISIN, which is how HAT names an
instrument. NSE's own bhavcopy carries both the ISIN and that exchange token (`FinInstrmId`,
`bhavcopy.nse_tokens`), so an instrument maps by (ISIN, series) -> NSE token -> the dump row with
that token. The row must also say what HAT expects, or no mapping is written (never one that
could route an order to another stock): an NSE equity row whose trading symbol is the
instrument's symbol (EQ) or the symbol with `-BE` (BE, trade-for-trade).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from .bhavcopy import (
    FORMAT_UDIFF,
    BhavcopyError,
    Fetcher,
    archive_name,
    archive_url,
    format_for,
    nse_tokens,
)
from .store import DataStore

__all__ = [
    "KITE_SERIES",
    "TOKEN_LOOKBACK_DAYS",
    "KiteListing",
    "KiteSymbolData",
    "bhavcopy_archive",
    "dump_rows",
    "kite_listings",
    "kite_symbol",
    "latest_nse_tokens",
]

KITE_SERIES = frozenset({"EQ", "BE"})  # the series HAT routes orders to, as for Fyers
_NSE = "NSE"
_EQUITY = "EQ"


@dataclass(frozen=True)
class KiteListing:
    """One NSE equity row of Kite's dump."""

    tradingsymbol: str
    instrument_token: int


def _token(value: object) -> int | None:
    """A positive whole token, as an int or its ASCII digits; None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        value = int(value.strip())
    return value if isinstance(value, int) and value > 0 else None


def dump_rows(text: str) -> list[dict[str, str]]:
    """The rows of Kite's instrument dump CSV (`instrument_token,exchange_token,tradingsymbol,
    ...,instrument_type,segment,exchange`)."""
    return list(csv.DictReader(io.StringIO(text)))


def kite_listings(rows: Iterable[Mapping[str, object]]) -> dict[int, KiteListing]:
    """NSE exchange token -> listing, of the NSE equity rows (`exchange` and `segment` NSE,
    `instrument_type` EQ) of Kite's instrument dump, as the SDK's `instruments("NSE")` returns it
    or as its CSV reads. A row without a readable token or symbol is left out, and so is a token
    two rows share: it cannot name one instrument."""
    found: dict[int, KiteListing] = {}
    clashes: set[int] = set()
    for row in rows:
        if (row.get("exchange"), row.get("segment"), row.get("instrument_type")) != (
            _NSE,
            _NSE,
            _EQUITY,
        ):
            continue
        exchange_token = _token(row.get("exchange_token"))
        instrument_token = _token(row.get("instrument_token"))
        symbol = row.get("tradingsymbol")
        if exchange_token is None or instrument_token is None:
            continue
        if not isinstance(symbol, str) or not symbol.strip():
            continue
        listing = KiteListing(symbol.strip(), instrument_token)
        if found.setdefault(exchange_token, listing) != listing:
            clashes.add(exchange_token)
    for token in clashes:
        del found[token]
    return found


def kite_symbol(
    isin: str,
    series: str,
    symbol: str,
    nse_tokens: Mapping[tuple[str, str], int],
    listings: Mapping[int, KiteListing],
) -> KiteListing | None:
    """The Kite listing of the instrument (`isin`, `series`, `symbol`), or None: a series HAT
    does not route to, no NSE token for (ISIN, series), no Kite row with that token, or a row whose
    trading symbol is not the one the series names."""
    if series not in KITE_SERIES:
        return None
    token = nse_tokens.get((isin.upper(), series))
    listing = listings.get(token) if token is not None else None
    if listing is None:
        return None
    expected = symbol if series == _EQUITY else f"{symbol}-{series}"
    return listing if listing.tradingsymbol == expected else None


TOKEN_LOOKBACK_DAYS = 10  # calendar days back the token list looks for a published bhavcopy

# (ISIN, series) -> NSE token, and NSE token -> Kite listing: what `refresh_kite_symbols` maps by
KiteSymbolData = tuple[Mapping[tuple[str, str], int], Mapping[int, KiteListing]]


def bhavcopy_archive(store: DataStore, fetch: Fetcher) -> Callable[[date], bytes | None]:
    """A day's UDiFF bhavcopy zip: the one kept in the store's `raw/`, else downloaded (not
    kept: the history build owns `raw/`); None when NSE has none for the day."""

    def read(day: date) -> bytes | None:
        name = archive_name(day)
        if store.raw_exists(name):
            return store.read_raw(name)
        result = fetch(archive_url(day))
        return result.data if result.status == "ok" and result.data else None

    return read


def latest_nse_tokens(
    read: Callable[[date], bytes | None], today: date, days: int = TOKEN_LOOKBACK_DAYS
) -> dict[tuple[str, str], int]:
    """The NSE tokens of the latest weekday bhavcopy `read` finds, from `today` back `days`
    calendar days; `BhavcopyError("bhavcopy_missing")` when none is found."""
    for back in range(days):
        day = today - timedelta(days=back)
        if day.weekday() >= 5 or format_for(day) != FORMAT_UDIFF:
            continue
        data = read(day)
        if data:
            return nse_tokens(data)
    raise BhavcopyError("bhavcopy_missing")
