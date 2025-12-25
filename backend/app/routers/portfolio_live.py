"""
New API endpoints for Portfolio Research & Live Monitoring System
"""

from fastapi import APIRouter, Depends, HTTPException
import psutil
from sqlalchemy.orm import Session
from typing import List, Dict, Any
from datetime import date
import logging

from ..database import (
    SessionLocal,
    StrategyContract as DBStrategyContract,
    PortfolioDailyState,
    AllocatorDecision,
    PaperFund,
    PaperPosition,
    PortfolioPolicy
)
from ..engines.strategy_contracts import (
    STRATEGY_CONTRACTS,
    get_compatible_strategies,
    get_contract,
    validate_backtest_config,
    ContractViolationError
)

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
    Get all strategy contracts from database.
    Returns contract information for UI display.
    """
    # Fetch all contracts from database
    db_contracts = db.query(DBStrategyContract).all()
    
    contracts_list = []
    for contract in db_contracts:
        contracts_list.append({
            "strategy_id": contract.strategy_id,
            "allowed_universes": contract.allowed_universes,
            "timeframe": contract.timeframe,
            "holding_period": contract.holding_period,
            "regime": contract.regime,
            "when_loses": contract.when_loses,
            "description": contract.description,
            "lifecycle_status": getattr(contract, "lifecycle_state", "RESEARCH") # Use getattr safely or map column name
        })
    
    return {"contracts": contracts_list}

@router.get("/compatible-strategies/{universe_id}")
def get_compatible_strategies_for_universe(
    universe_id: str,
    db: Session = Depends(get_db)
):
    """
    Get strategies compatible with a specific universe.
    UI uses this to disable incompatible strategies.
    """
    compatible = get_compatible_strategies(universe_id)
    
    return {
        "universe_id": universe_id,
        "compatible_strategies": compatible
    }

@router.post("/validate-config")
def validate_portfolio_config(
    config: Dict[str, Any],
    db: Session = Depends(get_db)
):
    """
    Validate strategy-universe compatibility before execution.
    Returns validation result with detailed error if invalid.
    """
    try:
        validate_backtest_config(config)
        return {
            "valid": True,
            "message": "Configuration is valid"
        }
    except ContractViolationError as e:
        return {
            "valid": False,
            "error": str(e)
        }

@router.get("/summary")
def get_portfolio_summary(db: Session = Depends(get_db)):
    """
    Get high-level portfolio summary for the dashboard.
    """
    try:
        # 1. Fetch Paper Funds
        funds = db.query(PaperFund).filter(PaperFund.user_id == 'default_user').first()
        if not funds:
            # Create default if not exists
            funds = PaperFund(user_id='default_user')
            db.add(funds)
            db.commit()
            db.refresh(funds)
            
        # 2. Fetch Active Positions P&L
        positions = db.query(PaperPosition).all()
        total_pnl = sum(p.pnl or 0 for p in positions)
        
        # 3. Allocation logic
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
    If run_id is provided, fetches the latest state for that specific run.
    """
    query = db.query(PortfolioDailyState)
    
    if run_id:
        query = query.filter(PortfolioDailyState.run_id == run_id)
        
    # If no date provided, use latest available for that run
    if date_param:
        target_date = date.fromisoformat(date_param)
        state = query.filter(PortfolioDailyState.date == target_date).first()
    else:
        state = query.order_by(PortfolioDailyState.date.desc()).first()
        
    if not state:
        # Instead of 404, we return empty state to handle UI gracefully if no run exists yet
        return None 
    
    decisions = decisions_query.all()
    
    # 3. Trust Map Data Calculation
    from ..database import BacktestDailyResult, BacktestRun
    import numpy as np
    
    trust_map = {}
    if state.run_id:
        # Get all strategies in this run
        run_config = db.query(BacktestRun).filter(BacktestRun.run_id == state.run_id).first()
        if run_config:
            strategy_ids = [s['id'] for s in (run_config.strategy_configs or [])]
            
            for s_id in strategy_ids:
                # Fetch recent performance for this strategy in this run
                daily_perf = db.query(BacktestDailyResult).filter(
                    BacktestDailyResult.run_id == state.run_id,
                    BacktestDailyResult.strategy_id == s_id
                ).order_by(BacktestDailyResult.date).all()
                
                # Calculate current drawdown
                equity = 1.0
                peak = 1.0
                current_dd = 0
                for day in daily_perf:
                    equity *= (1 + (day.daily_return or 0))
                    if equity > peak: peak = equity
                    current_dd = (equity - peak) / peak
                
                # Get Contract for baseline
                contract = STRATEGY_CONTRACTS.get(s_id)
                # Hardcoded "max expected DD" if not in contract (TODO: add to contract)
                expected_max_dd = -0.05 # Default 5% for now
                
                drift = (current_dd / expected_max_dd) if expected_max_dd != 0 else 0
                status = "STABLE"
                if drift > 0.8: status = "WARNING"
                if drift > 1.0: status = "VIOLATED"
                
                trust_map[s_id] = {
                    "strategy_id": s_id,
                    "current_drawdown": round(current_dd * 100, 2),
                    "expected_max_dd": round(expected_max_dd * 100, 2),
                    "drift_ratio": round(drift, 2),
                    "status": status,
                    "regime": contract.regime if contract else "UNKNOWN",
                    "weight": state.strategy_weights.get(s_id, 0) if state.strategy_weights else 0
                }
    
    decision_list = [{
        "strategy_id": d.strategy_id,
        "old_weight": d.old_weight,
        "new_weight": d.new_weight,
        "delta": d.delta,
        "reason": d.reason,
        "recovery_condition": d.recovery_condition,
        "severity": d.severity
    } for d in decisions]
    
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
    Includes equity curves, drawdowns, and risk metrics for the portfolio and strategies.
    """
    from ..database import BacktestDailyResult
    import numpy as np
    
    # 1. Fetch all daily states for the run
    daily_states = db.query(PortfolioDailyState).filter(
        PortfolioDailyState.run_id == run_id
    ).order_by(PortfolioDailyState.date).all()
    
    if not daily_states:
        raise HTTPException(status_code=404, detail="No data found for this run")

    dates = [str(s.date) for s in daily_states]
    equity_curve = [s.equity for s in daily_states]
    drawdown_curve = [s.drawdown for s in daily_states]
    
    # 2. Fetch per-strategy daily results
    strategy_results = db.query(BacktestDailyResult).filter(
        BacktestDailyResult.run_id == run_id
    ).order_by(BacktestDailyResult.date, BacktestDailyResult.strategy_id).all()
    
    # Group by strategy
    strategies_data = {}
    for result in strategy_results:
        if result.strategy_id not in strategies_data:
            strategies_data[result.strategy_id] = {
                "dates": [],
                "returns": [],
                "equity": [],
                "drawdown": []
            }
        
        strategies_data[result.strategy_id]["dates"].append(str(result.date))
        strategies_data[result.strategy_id]["returns"].append(result.daily_return)
    
    # 3. Calculate metrics per strategy
    strategies_metrics = {}
    portfolio_returns = np.array([d.drawdown - (daily_states[i-1].drawdown if i > 0 else 0) 
                                  for i, d in enumerate(daily_states)])
    
    for strategy_id, data in strategies_data.items():
        returns = np.array(data["returns"])
        
        # Build equity curve from returns
        equity = [1000000]  # Start with 1M
        for ret in returns:
            equity.append(equity[-1] * (1 + ret))
        
        # Calculate drawdown curve
        peak = equity[0]
        drawdowns = []
        dd_durations = []
        current_dd_duration = 0
        
        for eq in equity:
            if eq > peak:
                peak = eq
                current_dd_duration = 0
            else:
                current_dd_duration += 1
            
            dd = (eq - peak) / peak if peak > 0 else 0
            drawdowns.append(dd)
            if dd < 0:
                dd_durations.append(current_dd_duration)
        
        # Metrics
        max_dd = min(drawdowns) if drawdowns else 0
        sorted_dds = sorted([d for d in drawdowns if d < 0])
        p95_dd = sorted_dds[int(len(sorted_dds) * 0.95)] if len(sorted_dds) > 0 else 0
        longest_dd = max(dd_durations) if dd_durations else 0
        
        # DD speed classification (FAST if avg DD duration < 5 days)
        avg_dd_duration = np.mean(dd_durations) if dd_durations else 0
        dd_speed = "FAST" if avg_dd_duration < 5 else "SLOW"
        
        # Correlation to portfolio
        if len(returns) == len(portfolio_returns):
            correlation = np.corrcoef(returns, portfolio_returns[:len(returns)])[0, 1]
        else:
            correlation = 0.0
        
        strategies_metrics[strategy_id] = {
            "equity": equity[1:],  # Exclude initial capital
            "drawdown": [d * 100 for d in drawdowns[1:]],  # Convert to percentage
            "metrics": {
                "max_drawdown": max_dd * 100,
                "p95_dd": p95_dd * 100,
                "longest_dd_days": int(longest_dd),
                "dd_speed": dd_speed,
                "correlation_to_portfolio": float(correlation) if not np.isnan(correlation) else 0.0
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

@router.get("/universes")
def get_universes(db: Session = Depends(get_db)):
    """
    Get all defined universes for governance display.
    """
    from ..database import StockUniverse
    universes = db.query(StockUniverse).all()
    
    result = []
    for u in universes:
        # Get count of symbols for the latest date
        symbols_map = u.symbols_by_date or {}
        latest_date = max(symbols_map.keys()) if symbols_map else None
        symbol_count = len(symbols_map[latest_date]) if latest_date else 0
        
        result.append({
            "id": u.id,
            "description": u.description,
            "rebalance_frequency": u.rebalance_frequency,
            "symbol_count": symbol_count,
            "selection_rules": u.selection_rules,
            "created_at": str(u.created_at)
        })
    
    return result

@router.post("/strategies/lifecycle")
def update_strategy_lifecycle(
    strategy_id: str,
    new_state: str,
    db: Session = Depends(get_db)
):
    """
    Update strategy lifecycle state (e.g., Promote to LIVE).
    """
    from datetime import datetime
    strat = db.query(DBStrategyContract).filter(DBStrategyContract.strategy_id == strategy_id).first()
    if not strat:
        raise HTTPException(status_code=404, detail="Strategy not found")
    
    if new_state not in ["RESEARCH", "LIVE", "RETIRED"]:
        raise HTTPException(status_code=400, detail="Invalid lifecycle state")
        
    strat.lifecycle_state = new_state
    strat.state_since = datetime.now()
    if new_state == "LIVE":
        strat.approved_at = datetime.now()
        strat.approved_by = "admin" # Mock user for now
        
    db.commit()
    return {"message": f"Strategy {strategy_id} updated to {new_state}"}

@router.get("/active-runs")
def get_active_runs(db: Session = Depends(get_db)):
    """
    List all promoted backtest runs that are currently "Live".
    In this system, a 'Run' becomes a 'Live Portfolio' when tracked in monitoring.
    """
    from ..database import BacktestRun, PortfolioDailyState
    from sqlalchemy import func
    
    # We find runs that have at least one entry in portfolio_daily_state
    active_run_ids = db.query(PortfolioDailyState.run_id).distinct().all()
    active_run_ids = [r[0] for r in active_run_ids if r[0]]
    
    runs = db.query(BacktestRun).filter(BacktestRun.run_id.in_(active_run_ids)).all()
    
    result = []
    for r in runs:
        # Get latest state for this run
        latest_state = db.query(PortfolioDailyState).filter(
            PortfolioDailyState.run_id == r.run_id
        ).order_by(PortfolioDailyState.date.desc()).first()
        
        result.append({
            "run_id": r.run_id,
            "universe_id": r.universe_id,
            "start_date": str(r.start_date),
            "end_date": str(r.end_date),
            "current_equity": latest_state.equity if latest_state else 0,
            "current_drawdown": latest_state.drawdown if latest_state else 0,
            "status": "LIVE" if latest_state else "DORMANT"
        })
        
    return result


@router.get("/correlation/{run_id}")
def get_portfolio_correlation(run_id: str, db: Session = Depends(get_db)):
    """
    Calculate strategy correlation heatmap for a specific run.
    """
    from ..database import BacktestDailyResult
    import pandas as pd
    import numpy as np
    
    # Fetch returns for all strategies in the run
    results = db.query(BacktestDailyResult).filter(
        BacktestDailyResult.run_id == run_id
    ).all()
    
    if not results:
        raise HTTPException(status_code=404, detail="No data found for this run")
        
    # Pivot to get returns by date per strategy
    df = pd.DataFrame([{
        "date": r.date,
        "strategy_id": r.strategy_id,
        "return": r.daily_return
    } for r in results])
    
    pivot_df = df.pivot(index="date", columns="strategy_id", values="return").fillna(0)
    
    # Calculate correlation matrix
    corr_matrix = pivot_df.corr().fillna(0)
    
    # Format for UI (Heatmap)
    # [{ "id": "S1", "data": [{ "x": "S1", "y": 1 }, { "x": "S2", "y": 0.5 }] }]
    matrix_data = []
    strategies = corr_matrix.columns.tolist()
    
    for s_i in strategies:
        data_points = []
        for s_j in strategies:
            data_points.append({
                "x": s_j,
                "y": float(round(corr_matrix.loc[s_i, s_j], 3))
            })
        matrix_data.append({
            "id": s_i,
            "data": data_points
        })
        
    return matrix_data


@router.get("/risk-summary/{run_id}")
def get_portfolio_risk_summary(run_id: str, db: Session = Depends(get_db)):
    """
    Get aggregate risk summary for the portfolio (Section D).
    """
    from ..database import PortfolioDailyState
    import numpy as np
    
    states = db.query(PortfolioDailyState).filter(
        PortfolioDailyState.run_id == run_id
    ).order_by(PortfolioDailyState.date).all()
    
    if not states:
        raise HTTPException(status_code=404, detail="No data found for this run")
        
    drawdowns = [s.drawdown for s in states]
    max_dd = min(drawdowns) if drawdowns else 0
    
    # Duration (longest period in DD)
    longest_dd_duration = 0
    current_dd_duration = 0
    for dd in drawdowns:
        if dd < 0:
            current_dd_duration += 1
            longest_dd_duration = max(longest_dd_duration, current_dd_duration)
        else:
            current_dd_duration = 0
            
    # Worst Combined Regime (Regime with lowest avg return)
    # (Simplified: we use volatility regimes from PortfolioDailyState)
    regimes = {}
    for s in states:
        r = s.volatility_regime or "NORMAL"
        if r not in regimes: regimes[r] = []
        # Infer return from equity change
        # (This is a proxy since we don't store daily_return in PortfolioDailyState yet)
        regimes[r].append(s.drawdown) # Placeholder for return impact
        
    worst_regime = "NORMAL"
    min_avg_dd = 0
    for r, dds in regimes.items():
        avg_dd = sum(dds) / len(dds)
        if avg_dd < min_avg_dd:
            min_avg_dd = avg_dd
            worst_regime = r
            
    return {
        "max_drawdown": round(max_dd, 2),
        "drawdown_duration": longest_dd_duration,
        "worst_combined_regime": worst_regime,
        "risk_thresholds": "NORMAL", # Governance policy placeholder
        "active_governance": True
    }


@router.get("/policy")
def get_portfolio_policy(db: Session = Depends(get_db)):
    """
    Get current system-level portfolio policy.
    """
    policy = db.query(PortfolioPolicy).first()
    if not policy:
        # Create default policy
        policy = PortfolioPolicy(
            cash_reserve_pct=20.0,
            max_equity_exposure_pct=80.0,
            daily_stop_loss_pct=2.0,
            max_strategy_allocation_pct=25.0,
            updated_by="system"
        )
        db.add(policy)
        db.commit()
        db.refresh(policy)
        
    return {
        "cash_reserve_pct": policy.cash_reserve_pct,
        "max_equity_exposure_pct": policy.max_equity_exposure_pct,
        "daily_stop_loss_pct": policy.daily_stop_loss_pct,
        "max_strategy_allocation_pct": policy.max_strategy_allocation_pct,
        "last_updated": str(policy.last_updated),
        "updated_by": policy.updated_by
    }


@router.post("/policy")
def update_portfolio_policy(config: Dict[str, Any], db: Session = Depends(get_db)):
    """
    Update system-level portfolio policy.
    """
    policy = db.query(PortfolioPolicy).first()
    if not policy:
        policy = PortfolioPolicy()
        db.add(policy)
        
    policy.cash_reserve_pct = config.get("cash_reserve_pct", policy.cash_reserve_pct)
    policy.max_equity_exposure_pct = config.get("max_equity_exposure_pct", policy.max_equity_exposure_pct)
    policy.daily_stop_loss_pct = config.get("daily_stop_loss_pct", policy.daily_stop_loss_pct)
    policy.max_strategy_allocation_pct = config.get("max_strategy_allocation_pct", policy.max_strategy_allocation_pct)
    policy.updated_by = "admin" # Mock user
    
    db.commit()
    return {"message": "Policy updated successfully"}


@router.get("/system/health")
def get_system_health():
    """
    Get system health metrics for the monitoring dashboard.
    """
    return {
        "status": "ACTIVE",
        "cpu_usage": psutil.cpu_percent(),
        "memory_usage": psutil.virtual_memory().percent,
        "latency_ms": 12, # Simplified latency
        "uptime_days": 1.4
    }
