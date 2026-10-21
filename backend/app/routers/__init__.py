# Router exports for clean imports in main.py

from . import (
    activity,
    data_snapshot,
    market,
    market_dashboard,
    portfolio,
    system_health,
    terminal,
    unified,
    universe,
    upload,
    websocket,
)

__all__ = [
    "unified",
    "market",
    "market_dashboard",
    "system_health",
    "websocket",
    "universe",
    "upload",
    "data_snapshot",
    "portfolio",
    "terminal",
    "activity",
]
