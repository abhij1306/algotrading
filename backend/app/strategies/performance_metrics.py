"""
Performance Metrics Calculator
Calculate trading performance and risk metrics from backtest results
"""

from typing import List, Dict, Any
import pandas as pd
import numpy as np
from datetime import datetime

from .backtest_engine import Trade


class PerformanceMetrics:
    """
    Calculate comprehensive performance metrics for backtested strategies
    """
    
    def __init__(self, equity_curve: pd.DataFrame, trades: List[Trade], 
                 initial_capital: float, risk_free_rate: float = 0.06):
        """
        Initialize metrics calculator
        
        Args:
            equity_curve: DataFrame with columns ['timestamp', 'equity', 'drawdown']
            trades: List of Trade objects
            initial_capital: Starting capital
            risk_free_rate: Annual risk-free rate (default: 6% for India)
        """
        self.equity_curve = equity_curve
        self.trades = trades
        self.initial_capital = initial_capital
        self.risk_free_rate = risk_free_rate
    
    def calculate_all(self) -> Dict[str, Any]:
        """Calculate all performance metrics"""
        return {
            'performance': self._calculate_performance_metrics(),
            'risk': self._calculate_risk_metrics(),
            'trade_analysis': self._calculate_trade_metrics()
        }
    
    def _calculate_performance_metrics(self) -> Dict[str, float]:
        """Calculate performance-related metrics"""
        if self.equity_curve.empty:
            return {}
        
        # Calculate returns
        returns = self.equity_curve['equity'].pct_change().dropna()
        
        # Total return
        final_equity = self.equity_curve['equity'].iloc[-1]
        total_return = ((final_equity - self.initial_capital) / self.initial_capital) * 100
        
        # CAGR (Compound Annual Growth Rate)
        if len(self.equity_curve) > 1:
            start_date = self.equity_curve['timestamp'].iloc[0]
            end_date = self.equity_curve['timestamp'].iloc[-1]
            days = (end_date - start_date).days
            years = days / 365.25
            
            if years > 0:
                cagr = (((final_equity / self.initial_capital) ** (1 / years)) - 1) * 100
            else:
                cagr = 0
        else:
            cagr = 0
        
        # Sharpe Ratio
        if len(returns) > 0 and returns.std() > 0:
            excess_returns = returns - (self.risk_free_rate / 252)  # Daily risk-free rate
            sharpe_ratio = np.sqrt(252) * (excess_returns.mean() / returns.std())
        else:
            sharpe_ratio = 0
        
        # Sortino Ratio (focuses on downside deviation)
        if len(returns) > 0:
            downside_returns = returns[returns < 0]
            if len(downside_returns) > 0 and downside_returns.std() > 0:
                excess_returns = returns - (self.risk_free_rate / 252)
                sortino_ratio = np.sqrt(252) * (excess_returns.mean() / downside_returns.std())
            else:
                sortino_ratio = 0
        else:
            sortino_ratio = 0
        
        # Profit Factor
        winning_trades = [t for t in self.trades if t.pnl > 0]
        losing_trades = [t for t in self.trades if t.pnl < 0]
        
        gross_profit = sum([t.pnl for t in winning_trades]) if winning_trades else 0
        gross_loss = abs(sum([t.pnl for t in losing_trades])) if losing_trades else 0
        
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0
        
        return {
            'total_return_pct': round(total_return, 2),
            'cagr_pct': round(cagr, 2),
            'sharpe_ratio': round(sharpe_ratio, 2),
            'sortino_ratio': round(sortino_ratio, 2),
            'profit_factor': round(profit_factor, 2)
        }
    
    def _calculate_risk_metrics(self) -> Dict[str, Any]:
        """Calculate risk-related metrics"""
        if self.equity_curve.empty:
            return {}
        
        # Maximum Drawdown
        peak = self.equity_curve['equity'].expanding(min_periods=1).max()
        drawdown = ((self.equity_curve['equity'] - peak) / peak) * 100
        max_drawdown = drawdown.min()
        
        # Maximum drawdown duration
        # Find drawdown periods
        in_drawdown = drawdown < 0
        drawdown_periods = []
        start_idx = None
        
        for i, is_dd in enumerate(in_drawdown):
            if is_dd and start_idx is None:
                start_idx = i
            elif not is_dd and start_idx is not None:
                drawdown_periods.append((start_idx, i - 1))
                start_idx = None
        
        if start_idx is not None:
            drawdown_periods.append((start_idx, len(in_drawdown) - 1))
        
        max_dd_duration_days = 0
        if drawdown_periods:
            for start, end in drawdown_periods:
                duration = (self.equity_curve['timestamp'].iloc[end] - 
                          self.equity_curve['timestamp'].iloc[start]).days
                max_dd_duration_days = max(max_dd_duration_days, duration)
        
        # Volatility (annualized)
        returns = self.equity_curve['equity'].pct_change().dropna()
        volatility = returns.std() * np.sqrt(252) * 100 if len(returns) > 0 else 0
        
        # Max consecutive losses
        max_consecutive_losses = self._calculate_max_consecutive_losses()
        
        # Value at Risk (VaR) 95%
        if len(returns) > 0:
            var_95 = np.percentile(returns, 5) * 100
        else:
            var_95 = 0
        
        return {
            'max_drawdown_pct': round(max_drawdown, 2),
            'max_drawdown_duration_days': max_dd_duration_days,
            'volatility_pct': round(volatility, 2),
            'max_consecutive_losses': max_consecutive_losses,
            'var_95_pct': round(var_95, 2)
        }
    
    def _calculate_trade_metrics(self) -> Dict[str, Any]:
        """Calculate trade-level metrics"""
        if not self.trades:
            return {
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'win_rate_pct': 0,
                'avg_win': 0,
                'avg_loss': 0,
                'avg_win_pct': 0,
                'avg_loss_pct': 0,
                'largest_win': 0,
                'largest_loss': 0,
                'avg_trade_duration_minutes': 0,
                'expectancy': 0
            }
        
        winning_trades = [t for t in self.trades if t.pnl > 0]
        losing_trades = [t for t in self.trades if t.pnl < 0]
        
        total_trades = len(self.trades)
        win_rate = (len(winning_trades) / total_trades) * 100 if total_trades > 0 else 0
        
        avg_win = np.mean([t.pnl for t in winning_trades]) if winning_trades else 0
        avg_loss = np.mean([t.pnl for t in losing_trades]) if losing_trades else 0
        
        avg_win_pct = np.mean([t.pnl_pct for t in winning_trades]) if winning_trades else 0
        avg_loss_pct = np.mean([t.pnl_pct for t in losing_trades]) if losing_trades else 0
        
        largest_win = max([t.pnl for t in self.trades])
        largest_loss = min([t.pnl for t in self.trades])
        
        # Average trade duration
        durations = [(t.exit_time - t.entry_time).total_seconds() / 60 for t in self.trades]
        avg_duration = np.mean(durations) if durations else 0
        
        # Expectancy
        expectancy = (win_rate / 100) * avg_win + ((100 - win_rate) / 100) * avg_loss
        
        return {
            'total_trades': total_trades,
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate_pct': round(win_rate, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'avg_win_pct': round(avg_win_pct, 2),
            'avg_loss_pct': round(avg_loss_pct, 2),
            'largest_win': round(largest_win, 2),
            'largest_loss': round(largest_loss, 2),
            'avg_trade_duration_minutes': round(avg_duration, 2),
            'expectancy': round(expectancy, 2)
        }
    
    def _calculate_max_consecutive_losses(self) -> int:
        """Calculate maximum consecutive losing trades"""
        if not self.trades:
            return 0
        
        max_consecutive = 0
        current_consecutive = 0
        
        for trade in self.trades:
            if trade.pnl < 0:
                current_consecutive += 1
                max_consecutive = max(max_consecutive, current_consecutive)
            else:
                current_consecutive = 0
        
        return max_consecutive
