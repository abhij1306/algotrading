"""
Quant Backtest Wrapper - Institutional Vectorized Runner

For "institutional/fund" mode - uses locked strategy contracts from DB,
runs vectorized daily loops, and calculates institutional metrics.
"""

from typing import Dict, List, Any, Optional
from datetime import date, datetime, timedelta
from sqlalchemy.orm import Session
import pandas as pd
import numpy as np
import logging

from ...database import (
    StrategyContract,
    BacktestRun,
    BacktestDailyResult,
    PortfolioDailyResult
)
from ..strategies import STRATEGY_REGISTRY
from ..data_provider import DataProvider

logger = logging.getLogger(__name__)


class QuantBacktestRunner:
    """
    Vectorized backtest runner for institutional strategies.
    
    Unlike the event-driven BacktestCore (retail intraday), this runs
    daily loops and aggregates institutional metrics (CAGR, Profit Factor).
    """
    
    def __init__(self, db: Session):
        self.db = db
        self.data_provider = DataProvider(db)
    
    def run_strategy_backtest(
        self,
        strategy_id: str,
        universe_id: str,
        start_date: date,
        end_date: date,
        run_id: str,
        params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Run a single strategy backtest using vectorized daily loop.
        
        Args:
            strategy_id: Strategy identifier (e.g., "NIFTY_VOL_CONTRACTION")
            universe_id: Universe to trade (e.g., "NIFTY50")
            start_date: Backtest start date
            end_date: Backtest end date
            run_id: Unique run identifier for database persistence
            params: Optional strategy parameters
            
        Returns:
            Dictionary with metrics and daily results
        """
        # Validate strategy contract
        contract = self.db.query(StrategyContract).filter_by(
            strategy_id=strategy_id
        ).first()
        
        if not contract:
            raise ValueError(f"Strategy {strategy_id} not found in database")
        
        # Check universe compatibility
        if universe_id not in contract.allowed_universes:
            raise ValueError(
                f"Strategy {strategy_id} not compatible with universe {universe_id}. "
                f"Allowed: {contract.allowed_universes}"
            )
        
        # Get strategy class
        strategy_class = STRATEGY_REGISTRY.get(strategy_id)
        if not strategy_class:
            raise ValueError(f"Strategy implementation not found for {strategy_id}")
        
        # Initialize strategy
        merged_params = {**(contract.parameters or {}), **(params or {})}
        strategy = strategy_class(self.db, **merged_params)
        
        # Get symbols for universe
        symbols = self._get_universe_symbols(universe_id)
        if not symbols:
            raise ValueError(f"No symbols found for universe {universe_id}")
        
        logger.info(f"Running {strategy_id} on {len(symbols)} symbols from {start_date} to {end_date}")
        
        # Run vectorized daily loop
        daily_results = []
        cumulative_pnl = 0.0
        cumulative_equity = 100000.0  # Starting capital
        peak_equity = cumulative_equity
        max_drawdown = 0.0
        total_trades = 0
        winning_trades = 0
        losing_trades = 0
        gross_profit = 0.0
        gross_loss = 0.0
        
        current_date = start_date
        while current_date <= end_date:
            # Skip weekends
            if current_date.weekday() >= 5:
                current_date += timedelta(days=1)
                continue
            
            # Execute strategy for this day
            try:
                day_result = strategy.run_day(
                    current_date=current_date,
                    symbols=symbols,
                    data_provider=self.data_provider
                )
                
                # Extract metrics
                daily_return = day_result.get('daily_return', 0.0)
                daily_pnl = day_result.get('gross_pnl', 0.0)
                trades = day_result.get('trades', 0)
                win_rate = day_result.get('win_rate', 0.0)
                
                # Update cumulative metrics
                cumulative_pnl += daily_pnl
                cumulative_equity += daily_pnl
                
                # Track peak and drawdown
                if cumulative_equity > peak_equity:
                    peak_equity = cumulative_equity
                
                current_drawdown = (peak_equity - cumulative_equity) / peak_equity if peak_equity > 0 else 0.0
                max_drawdown = max(max_drawdown, current_drawdown)
                
                # Track trades
                total_trades += trades
                if daily_pnl > 0:
                    winning_trades += trades
                    gross_profit += daily_pnl
                elif daily_pnl < 0:
                    losing_trades += trades
                    gross_loss += abs(daily_pnl)
                
                # Save to database
                daily_record = BacktestDailyResult(
                    run_id=run_id,
                    strategy_id=strategy_id,
                    date=current_date,
                    daily_return=daily_return,
                    cumulative_return=(cumulative_equity - 100000) / 100000,
                    gross_pnl=daily_pnl,
                    net_pnl=daily_pnl * 0.997,  # After 0.3% costs
                    trades=trades,
                    drawdown=current_drawdown
                )
                daily_results.append(daily_record)
                
            except Exception as e:
                logger.warning(f"Error on {current_date} for {strategy_id}: {e}")
            
            current_date += timedelta(days=1)
        
        # Bulk insert daily results
        if daily_results:
            self.db.bulk_save_objects(daily_results)
            self.db.commit()
        
        # Calculate final metrics
        total_days = (end_date - start_date).days
        total_years = total_days / 365.0 if total_days > 0 else 1.0
        
        cagr = ((cumulative_equity / 100000) ** (1 / total_years) - 1) * 100 if total_years > 0 else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0
        win_rate_pct = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0
        
        metrics = {
            "total_return": (cumulative_equity - 100000) / 100000 * 100,
            "cagr": cagr,
            "max_drawdown": max_drawdown * 100,
            "profit_factor": profit_factor,
            "total_trades": total_trades,
            "win_rate": win_rate_pct,
            "sharpe_ratio": self._calculate_sharpe(daily_results),
            "final_equity": cumulative_equity
        }
        
        logger.info(f"✅ {strategy_id} completed: {total_trades} trades, {cagr:.2f}% CAGR, {max_drawdown*100:.2f}% Max DD")
        
        return {
            "strategy_id": strategy_id,
            "metrics": metrics,
            "daily_results": daily_results
        }
    
    def _get_universe_symbols(self, universe_id: str) -> List[str]:
        """Get symbols for a universe."""
        # For NIFTY strategies, just return the index
        if universe_id in ["NIFTY50", "NIFTY100", "BANKNIFTY"]:
            return [universe_id]
        
        # TODO: Implement proper universe lookup from database
        return [universe_id]
    
    def _calculate_sharpe(self, daily_results: List) -> float:
        """Calculate Sharpe ratio from daily results."""
        if not daily_results or len(daily_results) < 2:
            return 0.0
        
        returns = [r.daily_return for r in daily_results]
        if not returns:
            return 0.0
        
        mean_return = np.mean(returns)
        std_return = np.std(returns)
        
        if std_return == 0:
            return 0.0
        
        # Annualized Sharpe (assuming 252 trading days)
        sharpe = (mean_return / std_return) * np.sqrt(252)
        return sharpe
