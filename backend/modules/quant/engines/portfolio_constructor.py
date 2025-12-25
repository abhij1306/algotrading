
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
                            policy_id: Optional[str] = None,
                            policy_overrides: Optional[Dict[str, Any]] = None) -> None:
        
        # 1. Fetch Policy
        # Use database policy if provided, otherwise fallback to defaults/overrides
        policy = self.get_policy(policy_id)
        
        # Risk Thresholds from DB Policy or Overrides
        # We'll use daily_stop_loss_percent (from DB) as cautious and a 2x multiple as defensive 
        # OR we can assume specific fields for these in the future.
        # For now, let's map DB Policy fields to the Defensive Engine.
        cautious_limit = policy.daily_stop_loss_percent * -1.0 if policy else -2.0
        defensive_limit = cautious_limit * 2.0
        defensive_action = "scale_60" # Default
        corr_strength = policy.correlation_penalty.lower() if policy else "medium"
        
        max_alloc = policy.max_strategy_allocation_percent / 100.0 if policy else 1.0
        max_exposure = policy.max_equity_exposure_percent / 100.0 if policy else 1.0
        
        logger.info(f"[PortfolioConstructor] Constructing run={run_id} | Using Policy: {policy.name if policy else 'Default'}")
        
        # 2. Fetch daily returns for all selected strategies
        all_strategies_in_run = self.db.query(BacktestDailyResult.strategy_id).filter(
            BacktestDailyResult.run_id == run_id
        ).distinct().all()
        existing_strategy_ids = [s[0] for s in all_strategies_in_run]
        
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
        if not results: return

        # Pivot into a DataFrame [Date x Strategy]
        data = [{"date": r.date, "strategy_id": r.strategy_id, "daily_return": r.daily_return} for r in results]
        df = pd.DataFrame(data)
        returns_df = df.pivot(index="date", columns="strategy_id", values="daily_return").fillna(0.0)
        
        # 3. Iterate through dates and perform rolling allocation
        dates = sorted(returns_df.index.tolist())
        cumulative_equity = 1.0
        peak_equity = 1.0
        current_multiplier = 1.0 # For defensive scaling
        
        for i, current_date in enumerate(dates):
            # Calculate weights based on lookback
            if i < 1:
                weights = {sid: 1.0/len(valid_strategies) for sid in valid_strategies}
            else:
                window_start = max(0, i - lookback_window)
                history = returns_df.iloc[window_start:i]
                weights = self._calculate_weights(history, allocation_method, max_alloc, corr_strength)

            # --- DEFENSIVE POLICY ENGINE ---
            drawdown_pct = (cumulative_equity - peak_equity) / peak_equity * 100 if peak_equity > 0 else 0
            
            # Reset multiplier initially each day, then apply policy
            current_multiplier = 1.0
            
            if drawdown_pct <= defensive_limit:
                # Critical Drawdown
                if defensive_action == "scale_60": current_multiplier = 0.6
                elif defensive_action == "scale_40": current_multiplier = 0.4
                elif defensive_action == "freeze": current_multiplier = 0.1 # Minimum exposure
                elif defensive_action == "exit_all": current_multiplier = 0.0
            elif drawdown_pct <= cautious_limit:
                # Warning Drawdown
                current_multiplier = 0.8 # Standard cautious scaling
            
            # Apply multiplier to max_exposure
            final_weights = {k: v * max_exposure * current_multiplier for k, v in weights.items()}

            # Daily Portfolio Return
            daily_returns_vec = returns_df.loc[current_date]
            weights_vec = pd.Series(final_weights)
            
            common_idx = daily_returns_vec.index.intersection(weights_vec.index)
            portfolio_return = (daily_returns_vec[common_idx] * weights_vec[common_idx]).sum()
            
            cumulative_equity *= (1.0 + portfolio_return)
            peak_equity = max(peak_equity, cumulative_equity)
            drawdown = (cumulative_equity - peak_equity) / peak_equity
            
            # 4. Save Result
            self.db.add(PortfolioDailyResult(
                run_id=run_id,
                date=current_date,
                portfolio_return=float(portfolio_return),
                cumulative_equity=float(cumulative_equity),
                portfolio_drawdown=float(drawdown),
                strategy_weights=final_weights
            ))

        self.db.commit()

    def _calculate_weights(self, history: pd.DataFrame, method: str, max_alloc: float, corr_strength: str = "medium") -> Dict[str, float]:
        n = len(history.columns)
        if n == 0: return {}

        weights = {}
        
        if method == "EQUAL_WEIGHT":
            weights = {sid: 1.0/n for sid in history.columns}
            
        elif method == "INVERSE_VOLATILITY":
            vols = history.std().replace(0, 0.0001)
            inv_vols = 1.0 / vols
            weights = (inv_vols / inv_vols.sum()).to_dict()
            
        elif method == "CORRELATION_PENALIZED":
            vols = history.std().replace(0, 0.0001)
            corr = history.corr().fillna(0.0)
            avg_corr = corr.mean()
            
            # Apply strength multiplier to correlation penalty
            penalty_map = {"low": 0.5, "medium": 1.0, "high": 2.0}
            penalty_mult = penalty_map.get(corr_strength, 1.0)
            
            raw_weights = (1.0 / vols) * (1.0 - (avg_corr * penalty_mult))
            raw_weights = raw_weights.clip(lower=0.01) # Avoid negative weights
            
            if raw_weights.sum() == 0:
                 weights = {sid: 1.0/n for sid in history.columns}
            else:
                 weights = (raw_weights / raw_weights.sum()).to_dict()
        else:
            weights = {sid: 1.0/n for sid in history.columns}
            
        # Enforce Max Allocation Cap
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
