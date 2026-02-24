"""
Simple in-memory cache with TTL support
For production, consider Redis
"""

import time
from collections.abc import Callable
from functools import wraps
from typing import Any

_cache: dict[str, tuple[Any, float]] = {}


def cache_with_ttl(ttl_seconds: int = 300):
    """
    Decorator to cache function results with TTL.

    Args:
        ttl_seconds: Time to live in seconds (default 5 minutes)

    Example:
        @cache_with_ttl(ttl_seconds=3600)
        def get_index_symbols(index_id: str) -> list[str]:
            # Expensive operation
            return symbols
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Create cache key from function name and arguments
            cache_key = f"{func.__module__}.{func.__name__}:{str(args)}:{str(kwargs)}"

            # Check if cached and not expired
            if cache_key in _cache:
                value, expiry = _cache[cache_key]
                if time.time() < expiry:
                    return value

            # Call function and cache result
            result = func(*args, **kwargs)
            _cache[cache_key] = (result, time.time() + ttl_seconds)

            return result

        return wrapper

    return decorator


def clear_cache(pattern: str | None = None):
    """
    Clear cache entries.

    Args:
        pattern: If provided, only clear keys containing this pattern
    """
    global _cache

    if pattern is None:
        _cache.clear()
    else:
        keys_to_delete = [k for k in _cache.keys() if pattern in k]
        for key in keys_to_delete:
            del _cache[key]


def get_cache_stats() -> dict:
    """Get cache statistics"""
    now = time.time()
    active_entries = sum(1 for _, expiry in _cache.values() if expiry > now)
    expired_entries = len(_cache) - active_entries

    return {
        "total_entries": len(_cache),
        "active_entries": active_entries,
        "expired_entries": expired_entries,
    }
