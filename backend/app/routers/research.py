"""
Research APIs - MODERNIZED
Uses new QuantBacktestRunner architecture instead of legacy StrategyExecutor
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
from datetime import datetime
from pydantic import BaseModel

from ..database import get_db, StrategyContract, BacktestRun
from ..engines.backtest import QuantBacktestRunner

router = APIRouter(prefix="/api/research", tags=["Research"])


# ==================== REQUEST/RESPONSE MODELS ====================

class StrategyListItem(BaseModel):
    id: str
    name: str
    regime: str
    lifecycle_state: str
    state_since: Optional[str]


class PortfolioResearchRequest(BaseModel):
    name: str
    strategy_ids: List[str]
    universe_id: str
    start_date: str
    end_date: str
    allocation_method: str = "equal_weight"


# ==================== ENDPOINTS ====================

@router.get("/strategies", response_model=List[StrategyListItem])
async def get_strategies(db: Session = Depends(get_db)):
    """
    Get list of all strategies available for research
    MODERNIZED: Uses StrategyContract with lifecycle_state
    """
    strategies = db.query(StrategyContract).filter(
        StrategyContract.lifecycle_state.in_(['RESEARCH', 'PAPER', 'LIVE'])
    ).all()
    
    return [
        StrategyListItem(
            id=s.strategy_id,
            name=s.description or s.strategy_id.replace('_', ' ').title(),
            regime=s.regime,
            lifecycle_state=s.lifecycle_state or 'RESEARCH',
            state_since=s.state_since.isoformat() if s.state_since else None
        )
        for s in strategies
    ]


@router.get("/strategy/{strategy_id}")
async def get_strategy_detail(strategy_id: str, db: Session = Depends(get_db)):
    """
    Get detailed strategy information
    MODERNIZED: Returns contract details + latest backtest results
    """
    strategy = db.query(StrategyContract).filter_by(
        strategy_id=strategy_id
    ).first()
    
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    
    # Get latest backtest run for this strategy
    latest_run = db.query(BacktestRun).filter_by(
        strategy_id=strategy_id
    ).order_by(BacktestRun.created_at.desc()).first()
    
    return {
        "strategy_id": strategy.strategy_id,
        "description": strategy.description,
        "regime": strategy.regime,
        "timeframe": strategy.timeframe,
        "holding_period": strategy.holding_period,
        "lifecycle_state": strategy.lifecycle_state,
        "when_loses": strategy.when_loses,
        "allowed_universes": strategy.allowed_universes,
        "latest_backtest": {
            "run_id": latest_run.run_id if latest_run else None,
            "created_at": latest_run.created_at.isoformat() if latest_run else None,
            "total_return": latest_run.total_return if latest_run else None,
            "sharpe_ratio": latest_run.sharpe_ratio if latest_run else None
        } if latest_run else None
    }


@router.post("/portfolio")
async def create_research_portfolio(
    request: PortfolioResearchRequest,
    db: Session = Depends(get_db)
):
    """
    Execute portfolio research backtest
    MODERNIZED: Uses QuantBacktestRunner instead of legacy StrategyExecutor
    """
    try:
        start_date = datetime.strptime(request.start_date, "%Y-%m-%d")
        end_date = datetime.strptime(request.end_date, "%Y-%m-%d")
        
        runner = QuantBacktestRunner(db)
        
        # Run multi-strategy portfolio backtest
        result = await runner.run_portfolio(
            strategy_ids=request.strategy_ids,
            universe_id=request.universe_id,
            start_date=start_date,
            end_date=end_date,
            allocation={s: 1.0/len(request.strategy_ids) for s in request.strategy_ids}  # Equal weight
        )
        
        return {
            "name": request.name,
            "strategies": result['strategies'],
            "allocation": result['allocation'],
            "message": result['message']
        }
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        import traceback
        error_detail = f"{str(e)}\n{traceback.format_exc()}"
        raise HTTPException(status_code=500, detail=error_detail)


@router.get("/portfolios")
async def list_research_portfolios(db: Session = Depends(get_db)):
    """
    List all saved research portfolios
    """
    # Get unique run_ids from BacktestRun
    runs = db.query(BacktestRun).order_by(
        BacktestRun.created_at.desc()
    ).limit(50).all()
    
    return {
        "portfolios": [
            {
                "run_id": run.run_id,
                "strategy_id": run.strategy_id,
                "created_at": run.created_at.isoformat(),
                "total_return": run.total_return,
                "sharpe_ratio": run.sharpe_ratio,
                "max_drawdown": run.max_drawdown
            }
            for run in runs
        ]
    }
