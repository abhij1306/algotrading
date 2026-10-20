"""The screener: `ScreenerService` (service.py) over a SQL repository and live quotes."""

import functools
from typing import Any

from ...data_fetcher import fetch_fyers_quotes
from .cache import TTLCache
from .repository import SqlScreenerRepository
from .service import (
    InvalidSortFieldError,
    QuoteSource,
    ScreenerCriteria,
    ScreenerError,
    ScreenerRepository,
    ScreenerRequest,
    ScreenerRow,
    ScreenerService,
    SortSpec,
    UniverseDirectory,
    UniversesUnavailableError,
    UnknownUniverseError,
)

SCREENER_CACHE_TTL_SECONDS = 5.0
SCREENER_CACHE_MAXSIZE = 256


@functools.cache
def get_screener_cache() -> TTLCache[dict[str, Any]]:
    """The process-wide cache of screener result payloads; a FastAPI dependency."""
    return TTLCache(ttl_seconds=SCREENER_CACHE_TTL_SECONDS, maxsize=SCREENER_CACHE_MAXSIZE)


class FyersQuoteSource:
    """Live quotes from `data_fetcher.fetch_fyers_quotes` (empty when Fyers is off)."""

    def get_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        return fetch_fyers_quotes(symbols) or {}


@functools.cache
def get_quote_source() -> QuoteSource:
    """The screener's live quote source; a FastAPI dependency."""
    return FyersQuoteSource()


__all__ = [
    "SCREENER_CACHE_MAXSIZE",
    "SCREENER_CACHE_TTL_SECONDS",
    "FyersQuoteSource",
    "InvalidSortFieldError",
    "QuoteSource",
    "ScreenerCriteria",
    "ScreenerError",
    "ScreenerRepository",
    "ScreenerRequest",
    "ScreenerRow",
    "ScreenerService",
    "SortSpec",
    "SqlScreenerRepository",
    "TTLCache",
    "UniverseDirectory",
    "UniversesUnavailableError",
    "UnknownUniverseError",
    "get_quote_source",
    "get_screener_cache",
]
