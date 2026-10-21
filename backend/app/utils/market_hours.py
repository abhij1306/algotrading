"""
NSE market hours: the one owner of the session times, the weekend rule and DEV_MODE.

The cash session is 09:15:00-15:30:00 IST, Monday to Friday, both ends inclusive: 15:30:00
exactly still counts as open, any later instant is closed. `DEV_MODE=true` treats the market
as always open. NSE trading holidays come from `hat_trading_calendar`: `install_calendar`
(called at startup and after `calendar.refresh`) registers them, and every policy honours the
installed calendar on top of its own `holidays`. Special sessions (a trading day with its own
open and close, such as Muhurat or a Saturday session) are `sessions`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from .clock import SYSTEM_CLOCK, Clock, require_aware

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)


@dataclass(frozen=True)
class InstalledCalendar:
    """The calendar loaded from `hat_trading_calendar` (HAT-DATA-027, C7): holidays, special
    sessions and the market's default session."""

    holidays: frozenset[date] = frozenset()
    sessions: Mapping[date, tuple[time, time]] = field(default_factory=dict)
    session_open: time = SESSION_OPEN
    session_close: time = SESSION_CLOSE


# Replaced as a whole, so a reader sees either the old or the new calendar, never a mix.
_INSTALLED = InstalledCalendar()


def install_calendar(
    holidays: frozenset[date],
    sessions: Mapping[date, tuple[time, time]] | None = None,
    session_open: time = SESSION_OPEN,
    session_close: time = SESSION_CLOSE,
) -> None:
    """Register the exchange holidays, special sessions and default session every policy
    then honours."""
    global _INSTALLED  # noqa: PLW0603
    _INSTALLED = InstalledCalendar(
        frozenset(holidays), dict(sessions or {}), session_open, session_close
    )


def installed_calendar() -> InstalledCalendar:
    return _INSTALLED


class MarketPhase(StrEnum):
    DEV = "dev"
    WEEKEND = "weekend"
    HOLIDAY = "holiday"
    PRE_MARKET = "pre_market"
    OPEN = "open"
    POST_MARKET = "post_market"

    @property
    def is_open(self) -> bool:
        return self in (MarketPhase.OPEN, MarketPhase.DEV)


def dev_mode_enabled() -> bool:
    """True when the DEV_MODE env var is "true" (any case): market hours are bypassed."""
    return os.getenv("DEV_MODE", "False").lower() == "true"


@dataclass(frozen=True)
class MarketHoursPolicy:
    """When the NSE cash market is open.

    `clock` supplies "now" (inject a `FixedClock` in tests). `holidays` holds IST dates on
    which the exchange is closed; `sessions` maps an IST date to its own (open, close), which
    wins over the weekend and holiday rules. The installed calendar (`install_calendar`) applies
    to every policy in addition to these two.
    """

    clock: Clock = SYSTEM_CLOCK
    holidays: frozenset[date] = field(default_factory=frozenset)
    sessions: Mapping[date, tuple[time, time]] = field(default_factory=dict)
    session_open: time | None = None  # default session; None = the installed one (`hat_market`)
    session_close: time | None = None

    def now_ist(self) -> datetime:
        return self.clock.now().astimezone(IST)

    def phase(self, at: datetime | None = None, *, dev_mode: bool | None = None) -> MarketPhase:
        """The market phase at `at` (default: the clock's now).

        `dev_mode=None` reads the DEV_MODE env var on every call; pass a bool to use a
        setting the caller already read.
        """
        if dev_mode is None:
            dev_mode = dev_mode_enabled()
        if dev_mode:
            return MarketPhase.DEV
        now = self.now_ist() if at is None else require_aware(at).astimezone(IST)
        installed = _INSTALLED
        day = now.date()
        special = self.sessions.get(day) or installed.sessions.get(day)
        opens, closes = special or (
            self.session_open or installed.session_open,
            self.session_close or installed.session_close,
        )
        if special is None:
            if now.weekday() >= 5:  # Saturday or Sunday
                return MarketPhase.WEEKEND
            if day in self.holidays or day in installed.holidays:
                return MarketPhase.HOLIDAY
        current = now.time()
        if current < opens:
            return MarketPhase.PRE_MARKET
        if current > closes:
            return MarketPhase.POST_MARKET
        return MarketPhase.OPEN

    def is_open(self, at: datetime | None = None, *, dev_mode: bool | None = None) -> bool:
        return self.phase(at, dev_mode=dev_mode).is_open


DEFAULT_POLICY = MarketHoursPolicy()

_PHASE_MESSAGES: dict[MarketPhase, str] = {
    MarketPhase.DEV: "Market open (DEV_MODE)",
    MarketPhase.WEEKEND: "Market closed (Weekend)",
    MarketPhase.HOLIDAY: "Market closed (Holiday)",
    MarketPhase.PRE_MARKET: "Market closed (Pre-market)",
    MarketPhase.POST_MARKET: "Market closed (Post-market)",
    MarketPhase.OPEN: "Market open",
}


def is_market_open(policy: MarketHoursPolicy = DEFAULT_POLICY) -> tuple[bool, str]:
    """
    Check if NSE market is currently open.
    Supports DEV_MODE override.

    Returns:
        Tuple[bool, str]: (is_open, message)
    """
    phase = policy.phase()
    return phase.is_open, _PHASE_MESSAGES[phase]


def get_market_status(policy: MarketHoursPolicy = DEFAULT_POLICY) -> dict:
    """
    Get detailed market status information.

    Returns:
        dict with is_open, message, current_time_ist
    """
    now = policy.now_ist()
    phase = policy.phase(now)

    return {
        "is_open": phase.is_open,
        "message": _PHASE_MESSAGES[phase],
        "current_time_ist": now.strftime("%H:%M:%S IST"),
        "current_day": now.strftime("%A"),
        "market_open_time": f"{SESSION_OPEN:%H:%M} IST",
        "market_close_time": f"{SESSION_CLOSE:%H:%M} IST",
    }


def require_market_open() -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Decorator to ensure market is open before executing endpoint.
    Raises HTTPException(503) if market is closed.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        from functools import wraps

        from fastapi import HTTPException

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 - wraps any endpoint
            is_open, message = is_market_open()
            if not is_open:
                raise HTTPException(status_code=503, detail=f"Service unavailable: {message}")
            return await func(*args, **kwargs)

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 - wraps any endpoint
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
