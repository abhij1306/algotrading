"""
Shared utility helpers for the SmartTrader backend.
"""

from typing import Any


def safe_float(value: Any, default: float = 0.0) -> float:
    """
    Safely convert a value to float, returning a default on failure.

    Args:
        value: Value to convert (can be str, int, float, or None)
        default: Default value to return if conversion fails

    Returns:
        Float value or default if conversion fails
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    """
    Safely convert a value to int, returning a default on failure.

    Args:
        value: Value to convert (can be str, int, float, or None)
        default: Default value to return if conversion fails

    Returns:
        Int value or default if conversion fails
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
