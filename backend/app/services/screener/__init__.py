"""Screener services. The query and serialisation still live in `routers/screener.py`."""

import functools
from typing import Any

from .cache import TTLCache

SCREENER_CACHE_TTL_SECONDS = 5.0
SCREENER_CACHE_MAXSIZE = 256


@functools.cache
def get_screener_cache() -> TTLCache[dict[str, Any]]:
    """The process-wide cache of screener result payloads; a FastAPI dependency."""
    return TTLCache(ttl_seconds=SCREENER_CACHE_TTL_SECONDS, maxsize=SCREENER_CACHE_MAXSIZE)


__all__ = [
    "SCREENER_CACHE_MAXSIZE",
    "SCREENER_CACHE_TTL_SECONDS",
    "TTLCache",
    "get_screener_cache",
]
