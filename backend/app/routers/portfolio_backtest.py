from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Dict, Any
from datetime import date, datetime
import uuid
import logging
import pandas as pd

from ..database import (
    get_db,
    SessionLocal, 
    StockUniverse, 
    UserStockPortfolio, 
    BacktestRun,
    BacktestDailyResult,
    PortfolioDailyResult,
    StrategyContract as DBStrategyContract
)
from ..engines.universe_manager import UniverseManager
from ..engines.backtest.quant_wrapper import QuantBacktestRunner
from ..engines.portfolio_constructor import PortfolioConstructor
from ..engines.metrics_calculator import MetricsCalculator
from ..engines.strategy_contracts import (
    validate_backtest_config,
    get_contract,
    ContractViolationError
)

# Import Registry
from ..engines.strategies import STRATEGY_REGISTRY

router = APIRouter(prefix="/api/portfolio-backtest", tags=["Portfolio Backtest"])
logger = logging.getLogger(__name__)

@router.get("/universes")
async def get_universes(db: Session = Depends(get_db)):
    universes = db.query(StockUniverse).all()
    # Add default if missing
    if not universes:
        universes = [
            StockUniverse(id="NIFTY 50", description="Top 50", symbols_by_date={}, rebalance_frequency="NONE")
        ]
    return {
        "system_universes": [{"id": u.id, "name": u.id, "description": u.description} for u in universes],
        "user_portfolios": []
    }

@router.post("/run")
async def run_portfolio_backtest(
    config: Dict[str, Any], 
    db: Session = Depends(get_db)
):
    """
    Runs a multi-strategy backtest using QuantBacktestRunner.
    """
    try:
        universe_id = config.get("universe_id")
        strategies = config.get("strategies")
        allocation_method = config.get("allocation_method")
        lookback = config.get("lookback") or 30
        start_date_str = config.get("start_date")
        end_date_str = config.get("end_date")
        
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d")

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid config: {str(e)}")
    
    try:
        run_id = str(uuid.uuid4())
        
        # 1. Create Run Record
        backtest_run = BacktestRun(
            run_id=run_id,
            universe_id=universe_id,
            strategy_configs=strategies,
            portfolio_config={
                "allocation_method": allocation_method,
                "lookback": lookback
            },
            capital_mode="FIXED",
            start_date=start_date.date(),
            end_date=end_date.date()
        )
        db.add(backtest_run)
        db.commit()
        
        runner = QuantBacktestRunner(db)
        
        # 2. Run Each Strategy & Save Results
        strategy_ids = []
        for strat_config in strategies:
            strategy_id = strat_config["id"]
            if not strategy_id in STRATEGY_REGISTRY:
                logger.warning(f"Skipping unknown strategy {strategy_id}")
                continue

            strategy_ids.append(strategy_id)
            
            logger.info(f"Executing {strategy_id}...")
            result = await runner.run_single_strategy(
                strategy_id=strategy_id,
                universe_id=universe_id,
                start_date=start_date,
                end_date=end_date
            )
            
            # Save daily equity to DB for PortfolioConstructor
            daily_equity = result.get('daily_equity', [])
            
            for day_data in daily_equity:
                # day_data: {'date': '2024-01-01', 'equity': 1000000}
                # Calculate daily return from equity
                # Note: this is simplified. Ideally we store daily return directly.
                # But QuantBacktestRunner returns equity curve.
                pass
            
            # Re-calculate daily returns from equity curve
            df = pd.DataFrame(daily_equity)
            if not df.empty:
                df['date'] = pd.to_datetime(df['date'])
                df['daily_return'] = df['equity'].pct_change().fillna(0)

                for _, row in df.iterrows():
                    db_result = BacktestDailyResult(
                        run_id=run_id,
                        date=row['date'].date(),
                        strategy_id=strategy_id,
                        universe_id=universe_id,
                        daily_return=float(row['daily_return']),
                        gross_pnl=0, # Simplified
                        capital_allocated=float(row['equity']),
                        number_of_trades=0,
                        max_intraday_drawdown=0,
                        win_rate=0,
                        regime_tag="UNKNOWN"
                    )
                    db.add(db_result)

        db.commit()
    
        # 3. Portfolio Construction
        logger.info("Constructing Portfolio...")
        constructor = PortfolioConstructor(db)
        constructor.construct_portfolio(
            run_id=run_id,
            strategy_ids=strategy_ids,
            allocation_method=allocation_method,
            lookback_window=lookback
        )
        
        # 4. Calculate Summary Metrics
        port_results = db.query(PortfolioDailyResult).filter(PortfolioDailyResult.run_id == run_id).order_by(PortfolioDailyResult.date).all()
        if port_results:
            returns = pd.Series([r.portfolio_return for r in port_results])
            summary = MetricsCalculator.calculate_all(returns)
            backtest_run.summary_metrics = summary
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
        "equity_curve": [{"date": r.date.isoformat(), "equity": r.cumulative_equity, "drawdown": 0.0} for r in portfolio_daily]
    }
