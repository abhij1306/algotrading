"""The Fyers quote type and the one parser of the Fyers ``quotes`` REST payload.

A Fyers quotes reply looks like::

    {"s": "ok", "code": 200, "d": [
        {"n": "NSE:SBIN-EQ", "s": "ok", "v": {"lp": 812.5, "prev_close_price": 800.0,
            "open_price": 801.0, "high_price": 815.0, "low_price": 799.0,
            "volume": 1234567, "tt": 1760927400, "ch": 12.5, "chp": 1.56, ...}},
        {"n": "NSE:NOPE-EQ", "s": "error",
            "v": {"s": "error", "code": -300, "errmsg": "Please provide a valid symbol"}},
    ]}

Fyers fills a field it has no value for with 0, so a zero or negative price is read as
missing (None), never as a price. ``change`` and ``change_pct`` are derived from ``ltp`` and
``prev_close`` and are None unless both are known. The parser never raises: a reply that is
not ``s == "ok"`` gives no quotes, and an error entry or a malformed row is skipped.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from ..symbol_master import symbol_master

logger = logging.getLogger(__name__)

_HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class Quote:
    """One instrument's quote. ``symbol`` is the DB symbol; a missing value is None."""

    symbol: str
    ltp: Decimal | None
    change: Decimal | None
    change_pct: Decimal | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    prev_close: Decimal | None
    volume: int | None
    timestamp: datetime | None


def _decimal(value: object) -> Decimal | None:
    """``value`` as a finite Decimal, or None for missing, boolean or unparseable input."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _price(value: object) -> Decimal | None:
    """A positive price, or None: Fyers sends 0 for a price it does not have."""
    number = _decimal(value)
    return number if number is not None and number > 0 else None


def _volume(value: object) -> int | None:
    number = _decimal(value)
    if number is None or number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def _timestamp(value: object) -> datetime | None:
    """Fyers ``tt`` (epoch seconds) as an aware UTC datetime."""
    number = _decimal(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(int(number), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _is_error_entry(row: Mapping[str, object], values: Mapping[str, object]) -> bool:
    return row.get("s") == "error" or values.get("s") == "error" or "errmsg" in values


def parse_quote(row: object) -> Quote | None:
    """One ``d`` entry as a Quote, or None for an error entry or a malformed row."""
    if not isinstance(row, Mapping):
        return None
    name = row.get("n")
    values = row.get("v")
    if not isinstance(name, str) or not name.strip() or not isinstance(values, Mapping):
        return None
    if _is_error_entry(row, values):
        return None

    ltp = _price(values.get("lp"))
    prev_close = _price(values.get("prev_close_price"))
    change = change_pct = None
    if ltp is not None and prev_close is not None:
        change = ltp - prev_close
        change_pct = change / prev_close * _HUNDRED

    return Quote(
        symbol=symbol_master.to_db(name.strip()),
        ltp=ltp,
        change=change,
        change_pct=change_pct,
        open=_price(values.get("open_price")),
        high=_price(values.get("high_price")),
        low=_price(values.get("low_price")),
        prev_close=prev_close,
        volume=_volume(values.get("volume")),
        timestamp=_timestamp(values.get("tt")),
    )


def parse_quotes(response: object) -> dict[str, Quote]:
    """A Fyers quotes reply as {DB symbol: Quote}; {} for an error or malformed reply."""
    if not isinstance(response, Mapping) or response.get("s") != "ok":
        return {}
    rows = response.get("d")
    if isinstance(rows, Mapping):
        rows = [rows]
    if not isinstance(rows, list | tuple):
        return {}

    quotes: dict[str, Quote] = {}
    for row in rows:
        try:
            quote = parse_quote(row)
        except Exception:  # a bad row must never fail the batch
            logger.warning("Skipping a Fyers quote row that could not be parsed", exc_info=True)
            continue
        if quote is not None and quote.symbol:
            quotes[quote.symbol] = quote
    return quotes
