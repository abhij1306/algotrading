# Router exports for clean imports in main.py

from . import (
    activity,
    auth,
    backtest,
    data_snapshot,
    market,
    market_dashboard,
    portfolio,
    screener,
    system_health,
    terminal,
    unified,
    universe,
    upload,
    websocket,
)

__all__ = [
    "unified",
    "screener",
    "market",
    "market_dashboard",
    "auth",
    "system_health",
    "websocket",
    "universe",
    "upload",
    "backtest",
    "data_snapshot",
    "portfolio",
    "terminal",
    "activity"
]
