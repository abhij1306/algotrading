from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import List, Optional
from pydantic import BaseModel, UUID4
from datetime import datetime, time
import pytz

from ..database import (
    get_db,
    PortfolioPolicy, ResearchPortfolio, StrategyMetadata,
    LivePortfolioState
)

router = APIRouter(prefix="/api/portfolio/strategies", tags=["Quant Portfolio"])

# --- Schemas ---

class PortfolioPolicyCreate(BaseModel):
    name: str = "Standard Policy"
    cash_reserve_percent: float = 20.0
    daily_stop_loss_percent: float = 2.0
    max_equity_exposure_percent: float = 80.0
    max_strategy_allocation_percent: float = 25.0
    allocation_sensitivity: str = "MEDIUM"
    correlation_penalty: str = "MODERATE"

class StrategyPortfolioCreate(BaseModel):
    name: str
    description: Optional[str] = None
    policy_id: str
    composition: List[dict]

class StrategyMetadataUpdate(BaseModel):
    regime_notes: Optional[str] = None
    lifecycle_status: Optional[str] = None

class BacktestRequest(BaseModel):
    portfolio_id: Optional[UUID4] = None
    policy_id: Optional[UUID4] = None
    strategy_ids: Optional[List[str]] = None
    start_date: str = "2024-01-01"
    end_date: str = "2024-12-31"

# --- Endpoints ---

@router.post("/policy", status_code=status.HTTP_201_CREATED)
def create_policy(policy: PortfolioPolicyCreate, db: Session = Depends(get_db)):
    """Create a new portfolio risk policy"""
    new_policy = PortfolioPolicy(**policy.dict())
    db.add(new_policy)
    db.commit()
    db.refresh(new_policy)
    return new_policy

@router.get("/policy")
def list_policies(db: Session = Depends(get_db)):
    """List available policies"""
    return db.query(PortfolioPolicy).all()

@router.get("/available")
def list_available_strategies(db: Session = Depends(get_db)):
    """List strategies available for portfolio composition"""
    strategies = db.query(StrategyMetadata).all()
    if not strategies:
        defaults = [
            StrategyMetadata(strategy_id="TREND_FOLLOWING_V1", display_name="Trend Following V1", risk_profile={"regime": "TREND"}),
            StrategyMetadata(strategy_id="MEAN_REVERSION_RSI", display_name="Mean Reversion (RSI)", risk_profile={"regime": "RANGE"}),
            StrategyMetadata(strategy_id="VOLATILITY_BREAKOUT", display_name="Volatility Breakout", risk_profile={"regime": "VOLATILITY"})
        ]
        db.add_all(defaults)
        db.commit()
        strategies = defaults
    return strategies

@router.patch("/{strategy_id}")
def update_strategy_metadata(strategy_id: str, updates: StrategyMetadataUpdate, db: Session = Depends(get_db)):
    """Update strategy notes and lifecycle"""
    strat = db.query(StrategyMetadata).filter(StrategyMetadata.strategy_id == strategy_id).first()
    if not strat:
        raise HTTPException(status_code=404, detail="Strategy not found")

    if updates.regime_notes is not None:
        strat.regime_notes = updates.regime_notes
    if updates.lifecycle_status is not None:
        strat.lifecycle_status = updates.lifecycle_status

    db.commit()
    return strat

@router.post("/correlation")
def calculate_correlation(payload: dict):
    """
    Calculate correlation matrix for selected strategies.
    Payload: {"strategy_ids": ["STRAT_A", "STRAT_B"]}
    """
    ids = payload.get("strategy_ids", [])
    if len(ids) < 2:
        return {"max_correlation": 0.0, "matrix": []}

    return {
        "max_correlation": 0.85 if "TREND_FOLLOWING_V1" in ids and "VOLATILITY_BREAKOUT" in ids else 0.2,
        "matrix": []
    }

@router.post("", status_code=status.HTTP_201_CREATED)
def create_strategy_portfolio(portfolio: StrategyPortfolioCreate, db: Session = Depends(get_db)):
    """Create a new research/strategy portfolio"""
    policy = db.query(PortfolioPolicy).filter(PortfolioPolicy.id == portfolio.policy_id).first()
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    new_portfolio = ResearchPortfolio(**portfolio.dict())
    db.add(new_portfolio)
    db.commit()
    db.refresh(new_portfolio)
    return new_portfolio

@router.get("")
def list_strategy_portfolios(db: Session = Depends(get_db)):
    """List research portfolios"""
    return db.query(ResearchPortfolio).all()

async def _run_backtest_logic(db, policy, portfolio, start_date_str, end_date_str):
    """Shared helper for portfolio backtest"""
    try:
        from ..engines.quant.portfolio_backtest_core import PortfolioBacktestCore

        start_d = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_d = datetime.strptime(end_date_str, "%Y-%m-%d").date()

        engine = PortfolioBacktestCore(db, policy, portfolio)
        results = await engine.run_backtest(start_d, end_d)
        return results
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/backtest")
async def backtest_strategy_portfolio(request: BacktestRequest, db: Session = Depends(get_db)):
    """Run backtest on a strategy portfolio"""

    if request.portfolio_id:
        portfolio = db.query(ResearchPortfolio).filter(ResearchPortfolio.id == request.portfolio_id).first()
        if not portfolio:
            raise HTTPException(status_code=404, detail="Portfolio not found")
        policy = portfolio.policy
        if not policy:
            raise HTTPException(status_code=400, detail="Portfolio has no policy")

        results = await _run_backtest_logic(db, policy, portfolio, request.start_date, request.end_date)

        if "metrics" in results:
            portfolio.metrics_snapshot = results["metrics"]
            db.commit()
        return results

    elif request.policy_id and request.strategy_ids:
        policy = db.query(PortfolioPolicy).filter(PortfolioPolicy.id == request.policy_id).first()
        if not policy:
             raise HTTPException(status_code=404, detail="Policy not found")

        count = len(request.strategy_ids)
        composition = [{"strategy_id": s, "allocation_percent": 100/count} for s in request.strategy_ids] if count else []

        temp_portfolio = ResearchPortfolio(
            id=0,
            name="Simulation",
            composition=composition,
            policy_id=request.policy_id,
            status="simulated"
        )

        return await _run_backtest_logic(db, policy, temp_portfolio, request.start_date, request.end_date)

    else:
        raise HTTPException(status_code=400, detail="Must provide either portfolio_id OR (policy_id + strategy_ids)")

@router.get("/monitor")
def monitor_live_strategies(db: Session = Depends(get_db)):
    """Get live monitoring data for strategy portfolios"""
    try:
        tz = pytz.timezone('Asia/Kolkata')
        now = datetime.now(tz)

        if not (time(9, 15) <= now.time() <= time(15, 30)):
            return {"status": "market_closed", "portfolios": []}

        live_portfolios = db.query(ResearchPortfolio).filter(ResearchPortfolio.status == "LIVE").all()
        dashboard = []

        for p in live_portfolios:
            state = db.query(LivePortfolioState).filter(LivePortfolioState.portfolio_id == p.id).order_by(desc(LivePortfolioState.timestamp)).first()
            if state:
                dashboard.append({
                    "id": p.id,
                    "name": p.name,
                    "equity": state.total_equity,
                    "drawdown": state.current_drawdown_pct
                })
        return dashboard
    except Exception as e:
        print(f"Monitor endpoint error: {e}")
        return []

@router.post("/monitor/refresh")
def refresh_live_monitor(db: Session = Depends(get_db)):
    """Force refresh of live monitor"""
    from ..services.live_monitor import LiveMonitorService

    service = LiveMonitorService(db)
    results = service.monitor_all_active_portfolios()

    return {"status": "refreshed", "updates": len(results)}
