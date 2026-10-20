"""Frozen market hours for tests: the instants and the type of the `freeze_market` fixture."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.services.live_market_service import LiveMarketService
from app.utils.clock import FixedClock
from app.utils.market_hours import IST

# A Tuesday mid-session and a Saturday, both IST.
MARKET_OPEN_INSTANT = datetime(2026, 10, 20, 10, 0, tzinfo=IST)
MARKET_CLOSED_INSTANT = datetime(2026, 10, 24, 10, 0, tzinfo=IST)


class FreezeMarket(Protocol):
    """`freeze_market(True | False | instant, *live_services) -> FixedClock` (see conftest)."""

    def __call__(self, at: bool | datetime, /, *services: LiveMarketService) -> FixedClock: ...
