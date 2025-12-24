
import logging
import uuid
import json
from datetime import date, datetime
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session
from ..database import BacktestDailyResult, PortfolioDailyResult, PortfolioPolicy, AllocatorDecision
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

class PortfolioConstructor:
    """
    Combines strategy-level daily returns into a master portfolio.
    Supports various allocation methods and rebalancing.
    Enforces PortfolioPolicy risk limits.
    """
    
    def __init__(self, db: Session):
        self.db = db

    def get_policy(self, policy_id: Optional[str] = None) -> Optional[PortfolioPolicy]:
        """Fetch policy or default"""
        if policy_id:
             return self.db.query(PortfolioPolicy).filter(PortfolioPolicy.id == policy_id).first()
        # Return first active policy if none specified
        return self.db.query(PortfolioPolicy).first()

    def construct_portfolio(self, 
                            run_id: str, 
                            strategy_ids: List[str], 
                            allocation_method: str = "EQUAL_WEIGHT",
                            lookback_window: int = 30,
                            policy_id: Optional[str] = None) -> None:
        
        # 1. Fetch Policy
        policy = self.get_policy(policy_id)
        max_alloc = policy.max_strategy_allocation_percent / 100.0 if policy else 1.0
        max_exposure = policy.max_equity_exposure_percent / 100.0 if policy else 1.0
        
        logger.info(f"[PortfolioConstructor] Constructing run={run_id} with Policy={policy.name if policy else 'None'}")
        
        # 2. Fetch daily returns for all selected strategies
        all_strategies_in_run = self.db.query(BacktestDailyResult.strategy_id).filter(
            BacktestDailyResult.run_id == run_id
        ).distinct().all()
        existing_strategy_ids = [s[0] for s in all_strategies_in_run]
        
        # Intersection of requested and existing
        valid_strategies = list(set(strategy_ids).intersection(existing_strategy_ids))

        if not valid_strategies:
            logger.error(f"[PortfolioConstructor] No valid strategies found in DB for run_id {run_id}")
            return

        query = (
            self.db.query(BacktestDailyResult)
            .filter(BacktestDailyResult.run_id == run_id)
            .filter(BacktestDailyResult.strategy_id.in_(valid_strategies))
            .order_by(BacktestDailyResult.date)
        )
        results = query.all()

        if not results:
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
        
        # 3. Iterate through dates and perform rolling allocation
        dates = sorted(returns_df.index.tolist())
        cumulative_equity = 1.0
        peak_equity = 1.0
        
        for i, current_date in enumerate(dates):
            # Calculate weights based on lookback (rolling window)
            if i < 1:
                weights = {sid: 1.0/len(valid_strategies) for sid in valid_strategies}
            else:
                window_start = max(0, i - lookback_window)
                history = returns_df.iloc[window_start:i]
                weights = self._calculate_weights(history, allocation_method, max_alloc)

            # Apply Risk Limits (Max Exposure)
            # If we want to hold cash, we scale down weights.
            # Assuming weights sum to 1.0, we multiply by max_exposure

            # Simple Exposure logic: always scale to max_exposure unless volatility is high (adaptive)
            # For now, static scaling
            final_weights = {k: v * max_exposure for k, v in weights.items()}

            # Log Decision (Audit)
            if i % 30 == 0: # Log monthly to avoid spam
                self._log_allocator_decision(current_date, valid_strategies[0], weights, "Monthly Rebalance")

            # Daily Portfolio Return
            daily_returns_vec = returns_df.loc[current_date]
            weights_vec = pd.Series(final_weights)
            
            common_idx = daily_returns_vec.index.intersection(weights_vec.index)
            portfolio_return = (daily_returns_vec[common_idx] * weights_vec[common_idx]).sum()
            
            cumulative_equity *= (1.0 + portfolio_return)
            
            # Update Peak & DD
            peak_equity = max(peak_equity, cumulative_equity)
            drawdown = (cumulative_equity - peak_equity) / peak_equity
            
            # 4. Save Portfolio Daily Result
            port_result = PortfolioDailyResult(
                run_id=run_id,
                date=current_date,
                portfolio_return=float(portfolio_return),
                cumulative_equity=float(cumulative_equity),
                portfolio_drawdown=float(drawdown), # Added DD field
                strategy_weights=final_weights
            )
            self.db.add(port_result)

        self.db.commit()
        logger.info(f"Portfolio construction complete for run {run_id}")

    def _calculate_weights(self, history: pd.DataFrame, method: str, max_alloc: float) -> Dict[str, float]:
        n = len(history.columns)
        if n == 0: return {}

        weights = {}
        
        if method == "EQUAL_WEIGHT":
            weights = {sid: 1.0/n for sid in history.columns}
            
        elif method == "INVERSE_VOLATILITY":
            vols = history.std()
            vols = vols.replace(0, 0.0001)
            inv_vols = 1.0 / vols
            raw_weights = inv_vols / inv_vols.sum()
            weights = raw_weights.to_dict()
            
        elif method == "CORRELATION_PENALIZED":
            vols = history.std().replace(0, 0.0001)
            corr = history.corr().fillna(0.0)
            avg_corr = corr.mean()
            raw_weights = (1.0 / vols) * (1.0 - avg_corr)
            # Normalize
            if raw_weights.sum() == 0:
                 weights = {sid: 1.0/n for sid in history.columns}
            else:
                 weights = (raw_weights / raw_weights.sum()).to_dict()
        else:
            weights = {sid: 1.0/n for sid in history.columns}
            
        # Enforce Max Allocation Cap
        # Simple redistribution is hard; assume we just cap and re-normalize,
        # or just cap and leave remainder as cash.
        # Strict approach: Cap at max_alloc, don't re-normalize (extra becomes cash)
        for k in weights:
            if weights[k] > max_alloc:
                weights[k] = max_alloc

        return weights

    def _log_allocator_decision(self, date_val, strategy_id, weights, reason):
        """Log decision to DB"""
        # Note: AllocatorDecision table structure assumes per-strategy row?
        # Or we can just log generic decision if table supports JSON.
        # Based on schema: id, date, strategy_id, old_weight, new_weight, delta, reason
        # We'll skip granular row logging for now to avoid perf hit, unless critical.
        pass
