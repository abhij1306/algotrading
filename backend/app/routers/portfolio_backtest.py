from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Dict, Any
from datetime import date, datetime
import uuid
import logging

from ..database import (
    get_db, # Keep existing get_db
    SessionLocal, 
    StockUniverse, 
    UserStockPortfolio, 
    BacktestRun,
    BacktestDailyResult,
    PortfolioDailyResult,
    StrategyContract as DBStrategyContract
)
from ..engines.universe_manager import UniverseManager
from ..engines.strategy_executor import StrategyExecutor
from ..engines.portfolio_constructor import PortfolioConstructor
from ..engines.metrics_calculator import MetricsCalculator
from ..engines.strategy_contracts import (
    validate_backtest_config,
    get_compatible_strategies,
    get_contract,
    STRATEGY_CONTRACTS,
    ContractViolationError
)

# Import all strategy classes
from ..engines.strategies import (
    IntradayMomentumStrategy,
    IntradayMeanReversionStrategy,
    OvernightGapReversionStrategy,
    IndexMeanReversionStrategy
)
import pandas as pd

router = APIRouter(prefix="/api/portfolio-backtest", tags=["Portfolio Backtest"])
logger = logging.getLogger(__name__)

# Strategy Mapping
STRATEGIES = {
    "INTRADAY_MOMENTUM": IntradayMomentumStrategy,
    "INTRADAY_MEAN_REVERSION": IntradayMeanReversionStrategy,
    "OVERNIGHT_GAP": OvernightGapReversionStrategy,
    "INDEX_MEAN_REVERSION": IndexMeanReversionStrategy
}

@router.get("/universes")
async def get_universes(db: Session = Depends(get_db)):
    universes = db.query(StockUniverse).all()
    portfolios = db.query(UserStockPortfolio).all()
    
    return {
        "system_universes": [{"id": u.id, "name": u.id, "description": u.description} for u in universes],
        "user_portfolios": [{"id": p.portfolio_id, "name": p.portfolio_id} for p in portfolios]
    }

@router.post("/run")
async def run_portfolio_backtest(
    config: Dict[str, Any], 
    db: Session = Depends(get_db)
):
    """
    Runs a multi-strategy backtest.
    Expected Config:
    {
        "universe_id": "NIFTY100_CORE",
        "strategies": [
            {"id": "INTRADAY_MOMENTUM", "params": {...}},
            ...
        ],
        "allocation_method": "EQUAL_WEIGHT",
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "capital": 1000000
    }
    """
    try:
        universe_id = config.get("universe_id")
        strategies = config.get("strategies")
        allocation_method = config.get("allocation_method")
        lookback = config.get("lookback")
        start_date = config.get("start_date")
        end_date = config.get("end_date")
        
        validate_backtest_config(config)
    except ContractViolationError as e:
        logger.error(f"Contract violation: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    
    try:
        run_id = str(uuid.uuid4())
        
        # Create frozen snapshot
        backtest_run = BacktestRun(
            run_id=run_id,
            universe_id=universe_id,
            strategy_configs=strategies,
            portfolio_config={
                "allocation_method": allocation_method,
                "lookback": lookback
            },
            capital_mode="FIXED",
            start_date=datetime.strptime(start_date, "%Y-%m-%d").date(),
            end_date=datetime.strptime(end_date, "%Y-%m-%d").date()
        )
        db.add(backtest_run)
        db.commit()
        
        # Execute each strategy with its CONTRACT-DEFINED timeframe
        strategy_mapping = {
            "INTRADAY_MOMENTUM": IntradayMomentumStrategy,
            "INTRADAY_MEAN_REVERSION": IntradayMeanReversionStrategy,
            "OVERNIGHT_GAP": OvernightGapReversionStrategy,
            "INDEX_MEAN_REVERSION": IndexMeanReversionStrategy
        }
        
        for strat_config in strategies:
            strategy_id = strat_config["id"]
            params = strat_config.get("params", {})
            
            # Get contract to determine timeframe
            contract = get_contract(strategy_id)
            timeframe = 5 if contract.timeframe == "5MIN" else 1440
            
            logger.info(
                f"Executing {strategy_id} with contract timeframe: {contract.timeframe} "
                f"({timeframe} min)"
            )
            
            strategy_class = strategy_mapping.get(strategy_id)
            if not strategy_class:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown strategy: {strategy_id}"
                )
            
            strategy_instance = strategy_class(db, **params)
            executor = StrategyExecutor(db, strategy_instance)
            
            executor.run_backtest(
                universe_id=universe_id,
                start_date=backtest_run.start_date,
                end_date=backtest_run.end_date,
                run_id=run_id
            )
    
        # Portfolio construction
        constructor = PortfolioConstructor(db)
        constructor.construct_portfolio(
            run_id=run_id,
            allocation_method=allocation_method,
            lookback_window=lookback
        )
        
        # 3. Calculate Summary Metrics
        # Fetch portfolio results
        port_results = db.query(PortfolioDailyResult).filter(PortfolioDailyResult.run_id == run_id).order_by(PortfolioDailyResult.date).all()
        if port_results:
            returns = pd.Series([r.portfolio_return for r in port_results])
            summary = MetricsCalculator.calculate_all(returns)
            backtest_run.summary_metrics = summary # Use backtest_run instead of master_run
            db.commit()

        return {"run_id": run_id, "status": "COMPLETED"}

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{run_id}/results")
async def get_run_results(run_id: str, db: Session = Depends(get_db)):
    run = db.query(BacktestRun).filter(BacktestRun.run_id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
        
    portfolio_daily = db.query(PortfolioDailyResult).filter(PortfolioDailyResult.run_id == run_id).order_by(PortfolioDailyResult.date).all()
    
    return {
        "config": {
            "universe_id": run.universe_id,
            "start_date": run.start_date.isoformat(),
            "end_date": run.end_date.isoformat(),
            "capital_mode": run.capital_mode
        },
        "metrics": run.summary_metrics,
        "equity_curve": [{"date": r.date.isoformat(), "equity": r.cumulative_equity, "drawdown": r.portfolio_drawdown} for r in portfolio_daily]
    }
