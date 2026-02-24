"""
Market Hours Utility for Backend
Checks if NSE market is open (9:15 AM - 3:30 PM IST, Mon-Fri)
"""

import os
from datetime import datetime, time

import pytz

IST = pytz.timezone("Asia/Kolkata")


def is_market_open() -> tuple[bool, str]:
    """
    Check if NSE market is currently open.
    Supports DEV_MODE override.

    Returns:
        Tuple[bool, str]: (is_open, message)
    """
    # Check for DEV_MODE override
    dev_mode = os.getenv("DEV_MODE", "False").lower() == "true"
    if dev_mode:
        return True, "Market open (DEV_MODE)"

    now = datetime.now(IST)

    # Check if weekend
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False, "Market closed (Weekend)"

    # Check market hours: 9:15 AM to 3:30 PM IST
    market_open_time = time(9, 15)
    market_close_time = time(15, 30)
    current_time = now.time()

    if current_time < market_open_time:
        return False, "Market closed (Pre-market)"
    elif current_time > market_close_time:
        return False, "Market closed (Post-market)"
    else:
        return True, "Market open"


def get_market_status() -> dict:
    """
    Get detailed market status information.

    Returns:
        dict with is_open, message, current_time_ist
    """
    is_open, message = is_market_open()
    now = datetime.now(IST)

    return {
        "is_open": is_open,
        "message": message,
        "current_time_ist": now.strftime("%H:%M:%S IST"),
        "current_day": now.strftime("A"),
        "market_open_time": "09:15 IST",
        "market_close_time": "15:30 IST",
    }


def require_market_open():
    """
    Decorator to ensure market is open before executing endpoint.
    Raises HTTPException(503) if market is closed.
    """

    def decorator(func):
        from functools import wraps

        from fastapi import HTTPException

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            is_open, message = is_market_open()
            if not is_open:
                raise HTTPException(status_code=503, detail=f"Service unavailable: {message}")
            return await func(*args, **kwargs)

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            is_open, message = is_market_open()
            if not is_open:
                raise HTTPException(status_code=503, detail=f"Service unavailable: {message}")
            return func(*args, **kwargs)

        # Return appropriate wrapper based on function type
        import asyncio

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


def is_market_hours() -> bool:
    """
    Simple check if market is currently open.
    Returns True if market is open, False otherwise.
    """
    is_open, _ = is_market_open()
    return is_open
