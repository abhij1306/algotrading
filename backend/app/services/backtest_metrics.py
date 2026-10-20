"""Pure backtest metric calculations shared by every backtest path.

Two conventions coexist on purpose, because the persisted/API numbers of each
backtest must not change:

* ``sharpe_mean_std``: ``mean / std * sqrt(periods)`` of per-period returns, risk-free 0
  (phase-1 vectorised backtests).
* ``sharpe_excess_cagr``: ``(cagr - rf) / annualised std`` (VCP backtests).

No I/O, no pandas state beyond the inputs passed in.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class TradeStats:
    """Win/loss summary of a list of per-trade P&L (or return) values."""

    total: int
    wins: int
    losses: int
    win_rate: float  # fraction 0..1
    avg_win: float
    avg_loss: float
    profit_factor: float  # |gross win / gross loss|; 0.0 when there are no losers


def drawdown_series(equity: pd.Series) -> pd.Series:
    """Fractional drawdown from the running peak (values <= 0)."""
    if equity.empty:
        return equity.astype(float)
    return ((equity / equity.cummax()) - 1.0).fillna(0.0)


def max_drawdown(drawdown: pd.Series) -> float:
    """Worst drawdown as a non-positive fraction; 0.0 for an empty or flat series."""
    return float(drawdown.min()) if len(drawdown) else 0.0


def total_return(final_equity: float, initial_capital: float) -> float:
    """Fractional total return; 0.0 when the initial capital is not positive."""
    return (final_equity / initial_capital) - 1.0 if initial_capital > 0 else 0.0


def annualized_return(
    final_equity: float,
    initial_capital: float,
    n_periods: int,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """CAGR by period count: ``(final / initial) ** (periods_per_year / n_periods) - 1``."""
    if n_periods <= 0 or initial_capital <= 0:
        return 0.0
    return float((final_equity / initial_capital) ** (periods_per_year / max(1, n_periods)) - 1.0)


def sharpe_mean_std(returns: pd.Series, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """Annualised Sharpe with risk-free 0; 0.0 when volatility is zero."""
    std = float(returns.std(ddof=0)) if len(returns) else 0.0
    if std > 0:
        return float((returns.mean() / std) * np.sqrt(periods_per_year))
    return 0.0


def sharpe_excess_cagr(
    cagr: float,
    returns: pd.Series,
    risk_free: float,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """``(cagr - risk_free) / annualised std``; 0.0 when volatility is zero."""
    ann_std = float(returns.std(ddof=0) * np.sqrt(periods_per_year)) if len(returns) else 0.0
    return float((cagr - risk_free) / ann_std) if ann_std > 0 else 0.0


def trade_stats(values: Sequence[float]) -> TradeStats:
    """Summarise per-trade P&L or returns. A value of exactly 0 counts as a loss."""
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v <= 0]
    gross_loss = sum(losses)
    profit_factor = abs(sum(wins) / gross_loss) if losses and gross_loss != 0 else 0.0
    return TradeStats(
        total=len(values),
        wins=len(wins),
        losses=len(losses),
        win_rate=(len(wins) / len(values)) if values else 0.0,
        avg_win=(sum(wins) / len(wins)) if wins else 0.0,
        avg_loss=(sum(losses) / len(losses)) if losses else 0.0,
        profit_factor=float(profit_factor),
    )
