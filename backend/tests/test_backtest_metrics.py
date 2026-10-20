"""Golden-value and edge-case tests for the shared backtest metrics module.

Expected literals were captured from the pre-refactor calculators
(Phase1BacktestService._compute_metrics and the inline VCP block).
"""

import pandas as pd
import pytest

from app.services import backtest_metrics as m
from app.services.backtest_phase1_service import Phase1BacktestService


def _phase1_inputs() -> tuple[pd.Series, pd.Series, pd.Series]:
    idx = pd.date_range("2025-01-01", periods=6)
    ret = pd.Series([0.0, 0.02, -0.01, 0.03, -0.02, 0.01], index=idx)
    equity = 100000 * (1 + ret).cumprod()
    return equity, ret, m.drawdown_series(equity)


def test_phase1_metrics_match_pre_refactor_output() -> None:
    equity, ret, dd = _phase1_inputs()
    trades = [{"return_pct": x} for x in (5.0, -2.0, 3.5, 0.0)]
    svc = Phase1BacktestService.__new__(Phase1BacktestService)
    assert svc._compute_metrics(equity, ret, dd, trades, 100000.0) == {
        "initial_capital": 100000.0,
        "final_equity": 102948.5,
        "total_return_pct": 2.9485,
        "sharpe_ratio": 4.6476,
        "max_drawdown_pct": -2.0,
        "total_trades": 4,
        "winning_trades": 2,
        "losing_trades": 2,
        "win_rate_pct": 50.0,
    }


def test_phase1_metrics_empty_run() -> None:
    empty = pd.Series([], dtype=float)
    svc = Phase1BacktestService.__new__(Phase1BacktestService)
    out = svc._compute_metrics(empty, empty, empty, [], 100000.0)
    assert out["final_equity"] == 100000.0
    assert out["total_return_pct"] == 0.0
    assert out["sharpe_ratio"] == 0.0
    assert out["max_drawdown_pct"] == 0.0
    assert out["win_rate_pct"] == 0.0


def test_vcp_convention_matches_pre_refactor_output() -> None:
    equity = pd.Series([100000, 101000, 99500, 103000, 102000, 105000.0])
    returns = equity.pct_change().fillna(0.0)
    cagr = m.annualized_return(105000.0, 100000.0, len(equity))
    assert round(cagr * 100, 2) == 676.16
    assert round(abs(m.max_drawdown(m.drawdown_series(equity))) * 100, 2) == 1.49
    assert round(m.sharpe_excess_cagr(cagr, returns, risk_free=0.065), 4) == 22.5481
    stats = m.trade_stats([3000.0, -1500.0, 2500.0, -500.0])
    assert round(stats.profit_factor, 2) == 2.75
    assert (stats.wins, stats.losses, stats.win_rate) == (2, 2, 0.5)
    assert (stats.avg_win, stats.avg_loss) == (2750.0, -1000.0)
    assert round(m.total_return(105000.0, 100000.0) * 100, 2) == 5.0


def test_no_trades() -> None:
    stats = m.trade_stats([])
    assert stats == m.TradeStats(0, 0, 0, 0.0, 0.0, 0.0, 0.0)


def test_all_losers_and_breakeven_counts_as_loss() -> None:
    stats = m.trade_stats([-100.0, -300.0, 0.0])
    assert (stats.wins, stats.losses, stats.win_rate) == (0, 3, 0.0)
    assert stats.avg_loss == pytest.approx(-400.0 / 3)
    assert stats.profit_factor == 0.0


def test_all_winners_has_zero_profit_factor() -> None:
    stats = m.trade_stats([100.0, 50.0])
    assert stats.win_rate == 1.0
    assert stats.profit_factor == 0.0


def test_zero_drawdown_on_rising_equity() -> None:
    equity = pd.Series([100.0, 101.0, 102.0])
    assert m.max_drawdown(m.drawdown_series(equity)) == 0.0


def test_flat_equity_has_zero_sharpe_and_empty_series_is_safe() -> None:
    flat = pd.Series([0.0, 0.0, 0.0])
    assert m.sharpe_mean_std(flat) == 0.0
    assert m.sharpe_excess_cagr(0.1, flat, risk_free=0.065) == 0.0
    empty = pd.Series([], dtype=float)
    assert m.sharpe_mean_std(empty) == 0.0
    assert m.max_drawdown(m.drawdown_series(empty)) == 0.0
    assert m.annualized_return(100.0, 100.0, 0) == 0.0


def test_total_return_with_nonpositive_capital() -> None:
    assert m.total_return(100.0, 0.0) == 0.0
