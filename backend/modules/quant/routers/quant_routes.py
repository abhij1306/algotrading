"""
Quant Platform Router
Multi-strategy portfolio backtesting and optimization
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Dict, Any, List
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.parent.parent))

from app.database import get_db

router = APIRouter(prefix="/api/quant", tags=["quant"])

# Strategy Management
@router.get("/strategies")
async def list_strategies(db: Session = Depends(get_db)):
    """Get all available strategies in library"""
    try:
        from app.routers.portfolio import get_strategies
        return await get_strategies(db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/strategy-contracts")
async def get_strategy_contracts(db: Session = Depends(get_db)):
    """Get strategy contract definitions"""
    try:
        from app.routers.portfolio import get_strategy_contracts
        return await get_strategy_contracts(db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Portfolio Backtesting
@router.post("/backtest")
async def run_portfolio_backtest(
    config: Dict[str, Any],
    db: Session = Depends(get_db)
):
    """
    Run multi-strategy portfolio backtest
    
    Args:
        config: Backtest configuration with strategies, allocations, dates
        db: Database session
    
    Returns:
        Backtest results with metrics and equity curve
    """
    try:
        from ..engines.portfolio_backtest_core import run_portfolio_backtest
        
        result = await run_portfolio_backtest(config, db)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Governance
@router.get("/governance/policies")
async def get_risk_policies(db: Session = Depends(get_db)):
    """Get risk governance policies"""
    try:
        from app.routers.portfolio_backtest import get_policies
        return await get_policies(db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/governance/policies")
async def update_risk_policy(
    policy: Dict[str, Any],
    db: Session = Depends(get_db)
):
    """Update risk governance policy"""
    try:
        from app.routers.portfolio_backtest import update_policy
        return await update_policy(policy, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Monitoring
@router.get("/monitoring/live")
async def get_live_portfolio_status(db: Session = Depends(get_db)):
    """Get real-time portfolio monitoring data"""
    try:
        from app.routers.portfolio_live import get_monitoring_data
        return await get_monitoring_data(db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
