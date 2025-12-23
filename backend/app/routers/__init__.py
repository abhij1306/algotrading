# Router exports for clean imports in main.py
# This allows: from .routers import portfolio, market, etc.

from . import (
    unified,
    screener,
    market,
    market_dashboard,
    actions,
    auth,
    admin,
    portfolio,
    backtest,
    portfolio_live,
    research,
    portfolio_backtest,
    system_health,
    websocket
)

__all__ = [
    "unified",
    "screener",
    "market",
    "market_dashboard",
    "actions",
    "auth",
    "admin",
    "portfolio",
    "backtest",
    "portfolio_backtest",
    "portfolio_live",
    "research",
    "system_health",
    "websocket"
]
