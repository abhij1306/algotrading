
import numpy as np
import pandas as pd
from typing import Dict, Any

class MetricsCalculator:
    """
    Computes performance metrics for strategies and portfolios.
    Mandatory: CAGR, Sharpe, Sortino, MaxDD, Volatility, Win Rate, Expectancy.
    """
    
    @staticmethod
    def calculate_all(daily_returns: pd.Series) -> Dict[str, float]:
        if daily_returns.empty:
            return {}

        # 1. CAGR
        days = len(daily_returns)
        total_ret = (1 + daily_returns).prod()
        cagr = (total_ret ** (252.0 / days)) - 1 if days > 0 else 0.0

        # 2. Volatility (Annualized)
        vol = daily_returns.std() * np.sqrt(252)

        # 3. Sharpe Ratio (Risk-free = 0 for simplicity)
        sharpe = (daily_returns.mean() / daily_returns.std() * np.sqrt(252)) if daily_returns.std() != 0 else 0.0

        # 4. Sortino Ratio (Downside deviation)
        downside_returns = daily_returns[daily_returns < 0]
        downside_std = downside_returns.std() * np.sqrt(252)
        sortino = (daily_returns.mean() * 252 / downside_std) if downside_std != 0 else 0.0

        # 5. Max Drawdown
        cum_ret = (1 + daily_returns).cumprod()
        peak = cum_ret.cummax()
        dd = (cum_ret - peak) / peak
        max_dd = dd.min()

        # 6. Win Rate
        win_rate = (daily_returns > 0).sum() / len(daily_returns) if len(daily_returns) > 0 else 0.0

        # 7. Trade Expectancy (using daily as proxy if trade-level not available)
        avg_win = daily_returns[daily_returns > 0].mean() if (daily_returns > 0).any() else 0
        avg_loss = abs(daily_returns[daily_returns < 0].mean()) if (daily_returns < 0).any() else 0
        expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

        return {
            "cagr": float(cagr),
            "volatility": float(vol),
            "sharpe": float(sharpe),
            "sortino": float(sortino),
            "max_drawdown": float(max_dd),
            "win_rate": float(win_rate),
            "expectancy": float(expectancy)
        }
