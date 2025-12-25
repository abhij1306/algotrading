
from enum import Enum
from datetime import date, datetime, timedelta
import pandas as pd
import numpy as np
from typing import List, Dict, Optional
from sqlalchemy.orm import Session
from app.database import PortfolioPolicy, ResearchPortfolio

class RiskState(str, Enum):
    NORMAL = "NORMAL"
    CAUTIOUS = "CAUTIOUS"         # Reduced exposure due to drawdown
    DEFENSIVE = "DEFENSIVE"       # Minimal exposure, capital preservation
    HALTED = "HALTED"             # Stop trading due to breach

class PortfolioBacktestCore:
    """
    Risk-First Portfolio Backtest Engine.
    
    unlike standard backtesters that focus on strategy signals,
    this engine focuses on PORTFOLIO LEVEL RISK MANAGEMENT.
    
    It acts as a Governor:
    1. Monitors Daily Drawdown & Total Drawdown.
    2. Enforces PortfolioPolicy limits (Stop Loss, Max Exposure).
    3. Adjusts 'Allowed Allocation' dynamically based on RiskState.
    """
    
    def __init__(self, db: Session, policy: PortfolioPolicy, portfolio: ResearchPortfolio):
        self.db = db
        self.policy = policy
        self.portfolio = portfolio
        self.current_state = RiskState.NORMAL
        
        # State tracking
        self.equity = 1000000.0 # Start with 1M or configurable
        self.peak_equity = self.equity
        self.drawdown_pct = 0.0
        self.daily_pnl = 0.0
        
        # Metrics logs
        self.daily_logs = []
        
    async def run_backtest(self, start_date: date, end_date: date):
        """
        Execute the event-driven backtest loop with REAL strategy data.
        """
        from app.engines.backtest.quant_wrapper import QuantBacktestRunner
        from app.database import StrategyContract
        
        # 1. Fetch/Execute real backtests for all strategies in composition
        runner = QuantBacktestRunner(self.db)
        
        # Map: strategy_id -> daily_returns_series (pd.Series)
        strategy_returns = {}
        
        print(f"[Quant] Starting Portfolio Backtest {start_date} to {end_date}")
        
        # Determine universe for each strategy using simple heuristic (first allowed)
        # In robust system, universe is part of the composition definition
        for item in self.portfolio.composition:
            strat_id = item['strategy_id']
            contract = self.db.query(StrategyContract).filter_by(strategy_id=strat_id).first()
            if not contract:
                print(f"Warning: Strategy {strat_id} not found, skipping")
                continue
                
            universe_id = contract.allowed_universes[0] if contract.allowed_universes else 'NIFTY50'
            
            print(f"[Quant] Running strategy {strat_id} on {universe_id}...")
            
            try:
                # Run single strategy backtest
                # Convert date to datetime for the runner
                s_dt = datetime.combine(start_date, datetime.min.time())
                e_dt = datetime.combine(end_date, datetime.max.time())
                
                result = await runner.run_single_strategy(strat_id, universe_id, s_dt, e_dt)
                
                # Extract average daily return across all symbols in that strategy
                # Result contains 'symbol_results' -> list of BacktestResult
                # We need to aggregate equity curves to get strategy-level daily return
                
                # Simplified aggregation: Average the equity curves
                # (Assuming equal weight per symbol within strategy)
                if result.get('symbol_results'):
                    equity_curves = []
                    for res in result['symbol_results']:
                        # res is dict here
                        curve = pd.DataFrame(res['equity_curve'])
                        if not curve.empty:
                            curve['timestamp'] = pd.to_datetime(curve['timestamp'])
                            curve.set_index('timestamp', inplace=True)
                            equity_curves.append(curve['equity'])
                    
                    if equity_curves:
                        # Concat and mean along columns (axis=1)
                        agg_equity = pd.concat(equity_curves, axis=1).mean(axis=1)
                        # Calculate daily returns
                        strat_ret = agg_equity.pct_change().fillna(0)
                        strategy_returns[strat_id] = strat_ret
                
            except Exception as e:
                print(f"[Quant] Error running strategy {strat_id}: {e}")
                import traceback
                traceback.print_exc()

        # 2. Main Event Loop
        dates = pd.date_range(start_date, end_date, freq='B')
        current_equity = self.equity
        
        for d in dates:
            date_str = d.strftime("%Y-%m-%d")
            
            # A. Determine Allowed Exposure based on State (The Governor)
            allowed_equity_exposure = self._get_allowed_exposure() # Based on Risk State (Normal/Cautious/etc)
            
            # Policy Limit: Max Equity Exposure (e.g. 80%)
            # Also factor in Cash Reserve: Active Capital = Total Equity * (100 - CashReserve)%
            active_capital_ratio = (100.0 - self.policy.cash_reserve_percent) / 100.0
            
            # Effective Exposure Cap = Min(State Limit, Policy Limit, Real Cash Limit)
            effective_exposure_pct = min(allowed_equity_exposure, self.policy.max_equity_exposure_percent)
            effective_exposure_pct = min(effective_exposure_pct, active_capital_ratio * 100.0)

            # B. Aggregated Portfolio Return
            daily_gross_return = 0.0
            
            for item in self.portfolio.composition:
                s_id = item['strategy_id']
                # Policy Limit: Max Strategy Allocation (e.g. 25%)
                raw_weight = item['allocation_percent']
                capped_weight = min(raw_weight, self.policy.max_strategy_allocation_percent)
                
                weight = capped_weight / 100.0
                
                if s_id in strategy_returns:
                    try:
                        ret = 0.0
                        if d in strategy_returns[s_id].index:
                            ret = strategy_returns[s_id].loc[d]
                        
                        daily_gross_return += (ret * weight)
                    except KeyError:
                        pass
            
            # C. Apply Risk Governor (Exposure Cap)
            # Actual Return = Gross Return * (Effective Exposure / 100)
            actual_return = daily_gross_return * (effective_exposure_pct / 100.0) 
            
            # Stop Loss Check (Post-Calc for next day, or Intra-day simulation?)
            # Strictly speaking, Daily Stop Loss means "If we hit -2%, we flatten".
            # If actual_return < -stop_loss, we limit the loss at stop_loss (simulating close at cut)
            # AND we trigger Defensive state for next day.
            stop_loss_limit = -(self.policy.daily_stop_loss_percent / 100.0)
            if actual_return < stop_loss_limit:
                actual_return = stop_loss_limit # Hardout
                self.current_state = RiskState.CAUTIOUS # Penalty box
                
            # D. Update Equity

            
            # D. Update Equity
            pnl = current_equity * actual_return
            current_equity += pnl
            self.daily_pnl = pnl
            
            # E. Risk State Transition
            self._update_risk_state(current_equity)
            
            # F. Log Loop
            self.daily_logs.append({
                "date": date_str,
                "equity": current_equity,
                "drawdown": self.drawdown_pct,
                "state": self.current_state,
                "exposure": allowed_exposure,
                "daily_return": actual_return
            })
            
        return self._generate_results()

    def _get_allowed_exposure(self) -> float:
        """Calculate max capital allocation % based on current RiskState"""
        if self.current_state == RiskState.NORMAL:
            return self.policy.max_equity_exposure_percent
        elif self.current_state == RiskState.CAUTIOUS:
            # Cut exposure by half or to defined limit
            return max(self.policy.max_equity_exposure_percent * 0.5, 20.0)
        elif self.current_state == RiskState.DEFENSIVE:
            return 10.0 # Min exposure
        elif self.current_state == RiskState.HALTED:
            return 0.0
        return 0.0

    def _update_risk_state(self, current_equity: float):
        """Update drawdown and state transitions"""
        # Update Peak
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity
            self.drawdown_pct = 0.0
            
            # Recovery: If we were defensive/cautious, we might relax back to NORMAL
            # if (self.current_state != RiskState.HALTED):
            #     self.current_state = RiskState.NORMAL 
            # (Simple recovery logic: New High = Reset State)
            if self.current_state != RiskState.HALTED:
                 self.current_state = RiskState.NORMAL

        else:
            dd = (self.peak_equity - current_equity) / self.peak_equity * 100.0
            self.drawdown_pct = dd
            
            # State Transitions (Downside)
            # Thresholds based on Sensitivity
            sensitivity = self.policy.allocation_sensitivity # LOW, MEDIUM, HIGH
            
            cautious_limit = 10.0
            defensive_limit = 20.0
            
            if sensitivity == "HIGH": # Very reactive
                cautious_limit = 5.0
                defensive_limit = 10.0
            elif sensitivity == "LOW": # Tolerant
                cautious_limit = 15.0
                defensive_limit = 25.0
            
            if dd > defensive_limit:
                 self.current_state = RiskState.DEFENSIVE
            elif dd > cautious_limit:
                 self.current_state = RiskState.CAUTIOUS
                 
            # Check Daily Stop Loss
            daily_loss_pct = (self.daily_pnl / current_equity) * -100.0 if current_equity > 0 else 0
            if daily_loss_pct > self.policy.daily_stop_loss_percent:
                # Force Cautious state next day if daily loss limit hit
                if self.current_state == RiskState.NORMAL:
                    self.current_state = RiskState.CAUTIOUS

    def _generate_results(self):
        df = pd.DataFrame(self.daily_logs)
        if df.empty:
            return {}
            
        # Calculate Metrics
        total_ret = (df.iloc[-1]['equity'] - 1000000.0) / 1000000.0
        max_dd = df['drawdown'].max()
        
        return {
            "metrics": {
                "total_return": total_ret,
                "max_drawdown": max_dd,
                "final_equity": df.iloc[-1]['equity']
            },
            "daily_chart": df.to_dict(orient='records')
        }
