from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Dict, Any
from datetime import date, datetime
import logging
import uuid

from ..database import (
    get_db, 
    StockUniverse, 
    UserStockPortfolio, 
    BacktestRun
)
from ..engines.backtest.quant_wrapper import QuantBacktestRunner
from ..engines.portfolio_constructor import PortfolioConstructor
from ..engines.strategies import STRATEGY_REGISTRY

router = APIRouter(prefix="/api/portfolio-backtest", tags=["Portfolio Backtest"])
logger = logging.getLogger(__name__)

@router.get("/universes")
async def get_universes(db: Session = Depends(get_db)):
    """Fetch available universes for backtesting."""
    universes = db.query(StockUniverse).all()
    # Explicitly check for NIFTY indices which might not be in StockUniverse table yet if they are conceptual
    # But usually seed script adds them.
    # We also include User Portfolios if any
    portfolios = db.query(UserStockPortfolio).all()
    
    return {
        "system_universes": [{"id": u.id, "name": u.id, "description": u.description} for u in universes],
        "user_portfolios": [{"id": p.portfolio_id, "name": p.portfolio_id} for p in portfolios]
    }

@router.post("/run")
async def run_portfolio_backtest(
    request: dict,
    db: Session = Depends(get_db)
):
    """
    Execute a portfolio backtest using the QuantBacktestRunner.
    
    Args:
        request: {
            "universe_id": str,
            "strategies": [{"id": str, "params": dict}],
            "start_date": str,
            "end_date": str,
            "allocation_method": str,
            "lookback": int,
            "policy_settings": dict
        }
    """
    try:
        # Extract parameters
        universe_id = request.get("universe_id")
        strategies_config = request.get("strategies", [])
        start_date = datetime.strptime(request.get("start_date", "2024-01-01"), "%Y-%m-%d").date()
        end_date = datetime.strptime(request.get("end_date", "2024-12-31"), "%Y-%m-%d").date()
        allocation_method = request.get("allocation_method", "INVERSE_VOLATILITY")
        lookback = request.get("lookback", 30)
        
        if not universe_id or not strategies_config:
            raise HTTPException(status_code=400, detail="Universe and Strategies are required")

        # 1. Create Run Record
        run_id = str(uuid.uuid4())
        logger.info(f"Starting Backtest Run {run_id} for {universe_id}")
        
        backtest_run = BacktestRun(
            run_id=run_id,
            universe_id=universe_id,
            start_date=start_date,
            end_date=end_date,
            strategy_configs=strategies_config  # Correct column name
        )
        db.add(backtest_run)
        db.commit()
        
        # 2. Initialize Runner
        runner = QuantBacktestRunner(db)
        strategy_results = {}
        
        # 3. Run Each Strategy
        for strat in strategies_config:
            s_id = strat.get("id")
            s_params = strat.get("params", {})
            
            logger.info(f"Executing Strategy: {s_id}")
            result = runner.run_strategy_backtest(
                strategy_id=s_id,
                universe_id=universe_id,
                start_date=start_date,
                end_date=end_date,
                run_id=run_id,
                params=s_params
            )
            
            strategy_results[s_id] = result
            
        # 4. Construct Portfolio (Allocation & Risk Policy)
        # We invoke the constructor to combine strategy results into a master equity curve
        constructor = PortfolioConstructor(db)
        constructor.construct_portfolio(
            run_id=run_id,
            strategy_ids=[s.get("id") for s in strategies_config],
            allocation_method=allocation_method,
            lookback_window=lookback,
            policy_id=request.get("policy_id")
        )
        
        # Mark run complete by updating summary
        backtest_run.summary_metrics = {
            "strategies_executed": len(strategies_config),
            "allocation_method": allocation_method,
            "lookback": lookback,
            "completed_at": datetime.now().isoformat()
        }
        db.commit()
        
        return {
            "run_id": run_id,
            "status": "COMPLETED",
            "message": f"Successfully ran {len(strategies_config)} strategies and constructed portfolio."
        }

    except Exception as e:
        logger.error(f"Backtest Failed: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
