"""
Data fetching module for live Fyers quotes only.
Historical price ingestion from Fyers is deprecated in canonical mode.

`fetch_fyers_quotes` is a thin adapter over the gateway's `FyersClient.quotes()`: it keeps the
legacy dict shape (and a 5-second cache) for the screener, VCP positions and `DataProvider`.
"""

import logging
import time
from decimal import Decimal
from typing import Any

from .config import fyers_available
from .services.fyers.quotes import Quote
from .services.fyers_client import get_fyers_client

logger = logging.getLogger(__name__)

# Simple cache for quotes to avoid 429 errors
_quotes_cache = {}
_CACHE_TTL = 5  # 5 seconds TTL for live quotes (increased from 2 for better rate limiting)


def _number(value: Decimal | int | None) -> float | int:
    """A quote value as a JSON-friendly number; a missing value reads as 0 (legacy shape)."""
    if value is None:
        return 0
    return float(value) if isinstance(value, Decimal) else value


def _legacy_quote(quote: Quote) -> dict[str, Any]:
    return {
        "ltp": _number(quote.ltp),
        "volume": _number(quote.volume),
        "high": _number(quote.high),
        "low": _number(quote.low),
        "open": _number(quote.open),
        "prev_close": _number(quote.prev_close),
    }


def fetch_fyers_quotes(symbols: list) -> dict:
    """
    Fetch real-time quotes from Fyers API

    Args:
        symbols: List of stock symbols (without NSE: prefix)

    Returns:
        Dictionary of symbol -> quote data
    """
    if not fyers_available():
        logger.debug("[fetch_fyers_quotes] Fyers not configured")
        return {}

    now = time.time()
    result = {}
    symbols_to_fetch = []

    # Check cache first
    for symbol in symbols:
        if symbol in _quotes_cache:
            cache_time, data = _quotes_cache[symbol]
            if now - cache_time < _CACHE_TTL:
                result[symbol] = data
                continue
        symbols_to_fetch.append(symbol)

    if not symbols_to_fetch:
        logger.debug("[fetch_fyers_quotes] All %s symbols from cache", len(symbols))
        return result

    try:
        fyers_client = get_fyers_client()

        if not fyers_client:
            logger.warning("[fetch_fyers_quotes] Fyers client not available")
            return result

        logger.debug("[fetch_fyers_quotes] Fetching %s symbols", len(symbols_to_fetch))
        quotes = fyers_client.quotes(symbols_to_fetch)

        for symbol, quote in quotes.items():
            quote_data = _legacy_quote(quote)
            result[symbol] = quote_data
            _quotes_cache[symbol] = (now, quote_data)

        logger.debug("[fetch_fyers_quotes] Successfully fetched %s quotes", len(quotes))
        return result

    except Exception as e:
        logger.exception("[fetch_fyers_quotes] Error: %s", str(e))
        return {}
