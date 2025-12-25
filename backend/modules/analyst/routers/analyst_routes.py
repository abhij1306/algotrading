"""
Analyst Module Router
Single-stock analysis and portfolio risk evaluation
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Dict, Any
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.parent.parent))

from app.database import get_db

router = APIRouter(prefix="/api/analyst", tags=["analyst"])

@router.post("/backtest")
async def run_analyst_backtest(
    config: Dict[str, Any],
    db: Session = Depends(get_db)
):
    """
    Run single-stock backtest for strategy validation
    
    Args:
        config: Backtest configuration with symbol, dates, strategy
        db: Database session
    
    Returns:
        Backtest results with equity curve and metrics
    """
    try:
        from ..engines.analyst_wrapper import run_analyst_backtest
        
        result = await run_analyst_backtest(config, db)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/risk-analysis")
async def analyze_portfolio_risk(
    portfolio_id: int,
    lookback_days: int = 30,
    db: Session = Depends(get_db)
):
    """
    Analyze portfolio risk metrics
    
    Args:
        portfolio_id: Portfolio ID to analyze
        lookback_days: Historical lookback period
        db: Database session
    
    Returns:
        Risk metrics including volatility, correlation, VaR
    """
    try:
        # Import from existing app for now
        from app.portfolio_risk import calculate_portfolio_risk
        
        risk_data = await calculate_portfolio_risk(
            portfolio_id=portfolio_id,
            lookback_days=lookback_days,
            db=db
        )
        return risk_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
