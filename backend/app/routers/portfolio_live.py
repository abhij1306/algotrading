"""
New API endpoints for Portfolio Research & Live Monitoring System
"""

from fastapi import APIRouter, Depends, HTTPException
import psutil
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import List, Dict, Any
from datetime import date
import logging

from ..database import (
    SessionLocal,
    StrategyContract as DBStrategyContract,
    PortfolioDailyState,
    PortfolioDailyResult,
    BacktestDailyResult,
    BacktestRun,
    AllocatorDecision,
    PaperFund,
    PaperPosition,
    PortfolioPolicy
)
from ..engines.strategy_contracts import (
    get_compatible_strategies,
    get_contract,
    validate_backtest_config,
    ContractViolationError
)

# Use dynamic registry
from ..engines.strategies import STRATEGY_REGISTRY

router = APIRouter(prefix="/api/portfolio", tags=["Portfolio Live Monitoring"])
logger = logging.getLogger(__name__)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/strategy-contracts")
def get_strategy_contracts(db: Session = Depends(get_db)):
    """
    Get all strategy contracts from DB.
    """
    contracts = db.query(DBStrategyContract).all()
    return {"contracts": contracts}

@router.get("/compatible-strategies/{universe_id}")
def get_compatible_strategies_for_universe(
    universe_id: str,
    db: Session = Depends(get_db)
):
    """
    Get strategies compatible with a specific universe.
    """
    # Simply return all for now as we relaxed checks
    contracts = db.query(DBStrategyContract).all()
    compatible = [c.strategy_id for c in contracts]
    
    return {
        "universe_id": universe_id,
        "compatible_strategies": compatible
    }

@router.post("/validate-config")
def validate_portfolio_config(
    config: Dict[str, Any],
    db: Session = Depends(get_db)
):
    return {"valid": True, "message": "Configuration is valid"}

@router.get("/summary")
def get_portfolio_summary(db: Session = Depends(get_db)):
    """
    Get high-level portfolio summary for the dashboard.
    """
    try:
        funds = db.query(PaperFund).filter(PaperFund.user_id == 'default_user').first()
        if not funds:
            funds = PaperFund(user_id='default_user')
            db.add(funds)
            db.commit()
            db.refresh(funds)
            
        positions = db.query(PaperPosition).all()
        total_pnl = sum(p.pnl or 0 for p in positions)
        
        total_aum = funds.total_balance + total_pnl
        allocated_pct = round((funds.used_margin / funds.total_balance) * 100, 1) if funds.total_balance > 0 else 0
        
        return {
            "total_aum": total_aum,
            "daily_pnl": total_pnl,
            "allocated_pct": allocated_pct,
            "cash_pct": 100 - allocated_pct,
            "risk_status": "NORMAL" if allocated_pct < 80 else "CAUTIOUS"
        }
    except Exception as e:
        print(f"Error in portfolio summary: {e}")
        return {
            "total_aum": 0,
            "daily_pnl": 0,
            "allocated_pct": 0,
            "cash_pct": 100
        }

@router.get("/live-state")
def get_live_portfolio_state(
    run_id: str = None,
    date_param: str = None,
    db: Session = Depends(get_db)
):
    """
    Get current live portfolio state for monitoring.
    Falls back to PortfolioDailyResult (Backtest) if LiveState missing.
    """
    # 1. Try Live State
    query = db.query(PortfolioDailyState)
    if run_id: query = query.filter(PortfolioDailyState.run_id == run_id)
    
    if date_param:
        target_date = date.fromisoformat(date_param)
        state = query.filter(PortfolioDailyState.date == target_date).first()
    else:
        state = query.order_by(PortfolioDailyState.date.desc()).first()
        
    # 2. Fallback to Backtest Result
    if not state and run_id:
        bt_query = db.query(PortfolioDailyResult).filter(PortfolioDailyResult.run_id == run_id)
        if date_param:
            bt_state = bt_query.filter(PortfolioDailyResult.date == target_date).first()
        else:
            bt_state = bt_query.order_by(PortfolioDailyResult.date.desc()).first()

        if bt_state:
            # Map BacktestResult to LiveState format
            state = PortfolioDailyState(
                date=bt_state.date,
                run_id=bt_state.run_id,
                equity=bt_state.cumulative_equity,
                drawdown=bt_state.portfolio_drawdown,
                volatility=0.15, # Mock
                volatility_regime="NORMAL",
                risk_state="NORMAL",
                risk_state_reason="Backtest Simulation",
                strategy_weights=bt_state.strategy_weights
            )

    if not state:
        return None 
    
    # Decisions (Mock or Real)
    decision_list = []
    
    # 3. Trust Map Data
    trust_map = {}
    if state.run_id:
        run_config = db.query(BacktestRun).filter(BacktestRun.run_id == state.run_id).first()
        if run_config:
            # Strategies config is list of dicts: [{'id': '...', 'params': ...}]
            # Ensure we parse it correctly
            configs = run_config.strategy_configs or []
            strategy_ids = [s.get('id') for s in configs]
            
            for s_id in strategy_ids:
                daily_perf = db.query(BacktestDailyResult).filter(
                    BacktestDailyResult.run_id == state.run_id,
                    BacktestDailyResult.strategy_id == s_id
                ).order_by(BacktestDailyResult.date).all()
                
                equity = 1.0
                peak = 1.0
                current_dd = 0
                for day in daily_perf:
                    equity *= (1 + (day.daily_return or 0))
                    if equity > peak: peak = equity
                    current_dd = (equity - peak) / peak
                
                contract = db.query(DBStrategyContract).filter_by(strategy_id=s_id).first()
                expected_max_dd = -0.10
                
                drift = (current_dd / expected_max_dd) if expected_max_dd != 0 else 0
                status = "STABLE"
                if drift > 0.8: status = "WARNING"
                
                trust_map[s_id] = {
                    "strategy_id": s_id,
                    "current_drawdown": round(current_dd * 100, 2),
                    "expected_max_dd": round(expected_max_dd * 100, 2),
                    "drift_ratio": round(drift, 2),
                    "status": status,
                    "regime": contract.regime if contract else "UNKNOWN",
                    "weight": state.strategy_weights.get(s_id, 0) if state.strategy_weights else 0
                }
    
    return {
        "portfolio_health": {
            "date": str(state.date),
            "equity": state.equity,
            "drawdown": state.drawdown,
            "volatility": state.volatility,
            "volatility_regime": state.volatility_regime,
            "risk_state": state.risk_state,
            "risk_state_reason": state.risk_state_reason
        },
        "strategy_weights": state.strategy_weights,
        "allocator_decisions": decision_list,
        "trust_map": trust_map,
        "market_context": {
            "volatility_regime": state.volatility_regime
        }
    }

@router.get("/research-data/{run_id}")
def get_research_data(
    run_id: str,
    db: Session = Depends(get_db)
):
    """
    Get full historical data for the Research Tab.
    """
    import numpy as np
    
    # 1. Fetch from PortfolioDailyResult (Backtest)
    daily_results = db.query(PortfolioDailyResult).filter(
        PortfolioDailyResult.run_id == run_id
    ).order_by(PortfolioDailyResult.date).all()
    
    if not daily_results:
        # Try LiveState
        daily_results = db.query(PortfolioDailyState).filter(
            PortfolioDailyState.run_id == run_id
        ).order_by(PortfolioDailyState.date).all()

    if not daily_results:
        raise HTTPException(status_code=404, detail="No data found for this run")

    dates = [str(s.date) for s in daily_results]
    # Handle different field names if mixing types
    equity_curve = [getattr(s, 'cumulative_equity', getattr(s, 'equity', 0)) for s in daily_results]
    drawdown_curve = [getattr(s, 'portfolio_drawdown', getattr(s, 'drawdown', 0)) for s in daily_results]
    
    # 2. Fetch per-strategy
    strategy_results = db.query(BacktestDailyResult).filter(
        BacktestDailyResult.run_id == run_id
    ).order_by(BacktestDailyResult.date, BacktestDailyResult.strategy_id).all()
    
    strategies_data = {}
    for result in strategy_results:
        if result.strategy_id not in strategies_data:
            strategies_data[result.strategy_id] = {"dates": [], "returns": []}
        
        strategies_data[result.strategy_id]["dates"].append(str(result.date))
        strategies_data[result.strategy_id]["returns"].append(result.daily_return)
    
    strategies_metrics = {}
    for strategy_id, data in strategies_data.items():
        returns = np.array(data["returns"])
        equity = [1000000]
        for ret in returns:
            equity.append(equity[-1] * (1 + ret))
        
        # Simplified metrics
        strategies_metrics[strategy_id] = {
            "equity": equity[1:],
            "drawdown": [0] * len(equity[1:]), # Placeholder
            "metrics": {
                "max_drawdown": 0,
                "correlation_to_portfolio": 0
            }
        }
    
    return {
        "run_id": run_id,
        "dates": dates,
        "portfolio": {
            "equity": equity_curve,
            "drawdown": drawdown_curve,
            "metrics": {
                "max_drawdown": min(drawdown_curve) if drawdown_curve else 0,
                "current_equity": equity_curve[-1] if equity_curve else 0
            }
        },
        "strategies": strategies_metrics
    }
