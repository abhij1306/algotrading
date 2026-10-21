"""Pure backtest metric calculations shared by every backtest path.

The implementation lives in `app.hat.backtest.metrics` (K1); this module re-exports it so the
legacy backtests and HAT use one set of formulas (with HAT's volatility floor: a constant series
has a Sharpe of 0, not 1e17). Delete with the legacy code (K5).
"""

from __future__ import annotations

from app.hat.backtest.metrics import (
    TRADING_DAYS_PER_YEAR,
    TradeStats,
    annualized_return,
    drawdown_series,
    max_drawdown,
    sharpe_excess_cagr,
    sharpe_mean_std,
    total_return,
    trade_stats,
)

__all__ = [
    "TRADING_DAYS_PER_YEAR",
    "TradeStats",
    "annualized_return",
    "drawdown_series",
    "max_drawdown",
    "sharpe_excess_cagr",
    "sharpe_mean_std",
    "total_return",
    "trade_stats",
]
