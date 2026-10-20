"""Quote port for VCP positions: the last traded price per symbol, or None when unknown.

`QuoteSource` is the seam; `FyersQuoteSource` adapts today's `data_fetcher.fetch_fyers_quotes`.
A missing, zero or unparseable price is reported as None, never as zero, so callers
must decide explicitly what an unknown price means.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Protocol

from ... import data_fetcher


class QuoteSource(Protocol):
    def get_last_prices(self, symbols: Iterable[str]) -> dict[str, Decimal | None]:
        """One entry per requested DB symbol: its last price, or None when unavailable."""
        ...


def parse_price(value: object) -> Decimal | None:
    """`value` as a positive finite Decimal, or None for missing, zero, negative or bad input."""
    if value is None or isinstance(value, bool):
        return None
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        return None
    if not price.is_finite() or price <= 0:
        return None
    return price


class FyersQuoteSource:
    """Last prices from the Fyers quotes endpoint (5-second cache in `data_fetcher`)."""

    def get_last_prices(self, symbols: Iterable[str]) -> dict[str, Decimal | None]:
        wanted = list(dict.fromkeys(symbols))
        if not wanted:
            return {}
        quotes = data_fetcher.fetch_fyers_quotes(wanted)
        if not isinstance(quotes, dict):
            quotes = {}
        prices: dict[str, Decimal | None] = {}
        for symbol in wanted:
            quote = quotes.get(symbol)
            prices[symbol] = parse_price(quote.get("ltp")) if isinstance(quote, dict) else None
        return prices
