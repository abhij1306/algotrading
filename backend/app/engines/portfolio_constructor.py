
import logging
from datetime import date
from typing import List, Dict, Any
from sqlalchemy.orm import Session
from ..database import BacktestDailyResult, PortfolioDailyResult
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

class PortfolioConstructor:
    """
    Combines strategy-level daily returns into a master portfolio.
    Supports various allocation methods and rebalancing.
    """
    
    def __init__(self, db: Session):
        self.db = db

    def construct_portfolio(self, 
                            run_id: str, 
                            strategy_ids: List[str], 
                            allocation_method: str = "EQUAL_WEIGHT",
                            lookback_window: int = 30) -> None:
        
        
        # 1. Fetch daily returns for all selected strategies
        logger.info(f"[PortfolioConstructor] Fetching results for run_id={run_id}, strategy_ids={strategy_ids}")
        
        # DEBUG: Check what actually exists in the database for this run_id
        all_strategies_in_run = self.db.query(BacktestDailyResult.strategy_id).filter(
            BacktestDailyResult.run_id == run_id
        ).distinct().all()
        existing_strategy_ids = [s[0] for s in all_strategies_in_run]
        logger.info(f"[PortfolioConstructor] DEBUG - Strategies in DB for run_id {run_id}: {existing_strategy_ids}")
        logger.info(f"[PortfolioConstructor] DEBUG - Requested strategy_ids: {strategy_ids}")
        
        query = (
            self.db.query(BacktestDailyResult)
            .filter(BacktestDailyResult.run_id == run_id)
            .filter(BacktestDailyResult.strategy_id.in_(strategy_ids))
            .order_by(BacktestDailyResult.date)
        )

        results = query.all()
        logger.info(f"[PortfolioConstructor] Found {len(results)} backtest results for requested strategies")
        if not results:
            # Check if ANY results exist for this run_id
            all_for_run = self.db.query(BacktestDailyResult).filter(BacktestDailyResult.run_id == run_id).count()
            logger.error(f"[PortfolioConstructor  No results found for requested strategies {strategy_ids}")
            logger.error(f"[PortfolioConstructor] Total results for run_id: {all_for_run}")
            logger.error(f"[PortfolioConstructor] Strategies that exist: {existing_strategy_ids}")
            return

        # Pivot into a DataFrame [Date x Strategy]
        data = []
        for r in results:
            data.append({
                "date": r.date,
                "strategy_id": r.strategy_id,
                "daily_return": r.daily_return
            })
        
        df = pd.DataFrame(data)
        returns_df = df.pivot(index="date", columns="strategy_id", values="daily_return").fillna(0.0)
        
        # 2. Iterate through dates and perform rolling allocation
        dates = sorted(returns_df.index.tolist())
        cumulative_equity = 1.0
        
        for i, current_date in enumerate(dates):
            # Calculate weights based on lookback (rolling window)
            if i < 1:
                # Initial weights (Equal if not enough history)
                weights = {sid: 1.0/len(strategy_ids) for sid in strategy_ids}
            else:
                # Rolling window data
                window_start = max(0, i - lookback_window)
                history = returns_df.iloc[window_start:i]

                weights = self._calculate_weights(history, allocation_method)

            # Daily Portfolio Return
            daily_returns_vec = returns_df.loc[current_date]
            weights_vec = pd.Series(weights)
            
            # Ensure index alignment
            common_idx = daily_returns_vec.index.intersection(weights_vec.index)
            portfolio_return = (daily_returns_vec[common_idx] * weights_vec[common_idx]).sum()
            
            cumulative_equity *= (1.0 + portfolio_return)
            
            # Calculate Drawdown (simplified for now)
            # In a full impl, we'd track high-water mark
            
            # 3. Save Portfolio Daily Result
            port_result = PortfolioDailyResult(
                run_id=run_id,
                date=current_date,
                portfolio_return=float(portfolio_return),
                cumulative_equity=float(cumulative_equity),
                strategy_weights=weights # Dict of weights
            )
            self.db.add(port_result)

        self.db.commit()
        logger.info(f"Portfolio construction complete for run {run_id}")

    def _calculate_weights(self, history: pd.DataFrame, method: str) -> Dict[str, float]:
        n = len(history.columns)
        
        if method == "EQUAL_WEIGHT":
            return {sid: 1.0/n for sid in history.columns}
            
        elif method == "INVERSE_VOLATILITY":
            vols = history.std()
            vols = vols.replace(0, 0.0001) # Avoid div by zero
            inv_vols = 1.0 / vols
            weights = inv_vols / inv_vols.sum()
            return weights.to_dict()
            
        elif method == "CORRELATION_PENALIZED":
            # Simplified approach: Penalize highly correlated strategies
            # In real impl, use Hierarchical Risk Parity or similar
            vols = history.std().replace(0, 0.0001)
            corr = history.corr().fillna(0.0)
            avg_corr = corr.mean()

            raw_weights = (1.0 / vols) * (1.0 - avg_corr)
            weights = raw_weights / raw_weights.sum()
            return weights.fillna(1.0/n).to_dict()
            
        return {sid: 1.0/n for sid in history.columns}
