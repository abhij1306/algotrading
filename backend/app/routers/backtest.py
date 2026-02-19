"""
Backtest Router
==============
API endpoints for backtesting functionality.
"""
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..engines.backtest_engine import BacktestConfig, run_backtest

router = APIRouter()
DB_DEPENDENCY = Depends(get_db)


class BacktestRequest(BaseModel):
    """Request model for portfolio backtesting"""
    # Universe settings
    universe: str = "NIFTY50"
    start_date: str  # YYYY-MM-DD
    end_date: str    # YYYY-MM-DD

    # Capital settings
    initial_capital: float = 1000000.0

    # Strategy settings
    strategy: str = "momentum"
    rebalance_frequency: str = "monthly"

    # Position settings
    max_positions: int = 10

    # Costs
    brokerage: float = 0.001
    slippage: float = 0.0005


class SingleSymbolBacktestRequest(BaseModel):
    """Request model for single-symbol strategy backtesting"""
    strategy_name: str
    symbol: str
    start_date: str  # YYYY-MM-DD
    end_date: str    # YYYY-MM-DD
    timeframe: str = "5min"
    initial_capital: float = 100000
    params: dict[str, Any] | None = {}


@router.get("/strategies")
async def list_strategies():
    """List all available trading strategies"""
    strategies = [
        {
            "id": "momentum",
            "name": "Momentum",
            "description": "Buy strongest performers, rebalance monthly",
            "universes": ["NIFTY50", "NIFTY100", "NIFTY200", "NIFTY500"]
        },
        {
            "id": "mean_reversion",
            "name": "Mean Reversion",
            "description": "Buy oversold stocks, sell overbought",
            "universes": ["NIFTY50", "NIFTY100"]
        },
        {
            "id": "value",
            "name": "Value",
            "description": "Buy undervalued stocks",
            "universes": ["NIFTY50", "NIFTY100", "NIFTY500"]
        }
    ]
    return {"strategies": strategies}


@router.get("/universes")
async def list_universes(db: Session = DB_DEPENDENCY):
    """List all available universes"""
    from ..models.universe import IndexUniverseDefinition

    universes = db.query(IndexUniverseDefinition).filter(
        IndexUniverseDefinition.is_custom.is_(False)
    ).all()

    return {
        "universes": [
            {
                "code": u.index_code,
                "name": u.index_name,
                "exchange": u.exchange
            }
            for u in universes
        ]
    }


@router.post("/run")
async def run_portfolio_backtest(request: BacktestRequest, db: Session = DB_DEPENDENCY):
    """Run portfolio-level backtest with universe reconstruction"""
    try:
        # Parse dates
        start_date = datetime.strptime(request.start_date, "%Y-%m-%d").date()
        end_date = datetime.strptime(request.end_date, "%Y-%m-%d").date()

        # Create config
        config = BacktestConfig(
            start_date=start_date,
            end_date=end_date,
            initial_capital=request.initial_capital,
            universe=request.universe,
            strategy=request.strategy,
            rebalance_frequency=request.rebalance_frequency,
            max_positions=request.max_positions,
            brokerage=request.brokerage,
            slippage=request.slippage,
        )

        # Run backtest
        result = run_backtest(config, save_to_db=True)

        return {
            "status": "success",
            "run_id": result.run_id,
            "config": {
                "universe": request.universe,
                "start_date": request.start_date,
                "end_date": request.end_date,
                "initial_capital": request.initial_capital,
            },
            "metrics": result.metrics,
            "total_trades": len(result.trades),
        }

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"Backtest error: {tb}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/results/{run_id}")
async def get_backtest_results(run_id: str, db: Session = DB_DEPENDENCY):
    """Get backtest results by run ID"""
    from ..models.backtest import BacktestDailyResult, BacktestRun

    # Get run
    run = db.query(BacktestRun).filter(BacktestRun.run_id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Backtest run not found")

    # Get daily results
    daily_results = db.query(BacktestDailyResult).filter(
        BacktestDailyResult.run_id == run_id
    ).order_by(BacktestDailyResult.date).all()

    return {
        "run": {
            "run_id": run.run_id,
            "strategy_id": run.strategy_id,
            "universe": run.universe,
            "start_date": run.start_date,
            "end_date": run.end_date,
            "initial_capital": run.initial_capital,
            "final_capital": run.final_capital,
            "total_return": run.total_return,
            "sharpe_ratio": run.sharpe_ratio,
            "max_drawdown": run.max_drawdown,
            "status": run.status,
        },
        "daily_results": [
            {
                "date": r.date,
                "equity": r.equity,
                "cash": r.cash,
                "positions_count": r.positions_count,
                "daily_return": r.daily_return,
            }
            for r in daily_results
        ]
    }


@router.get("/runs")
async def list_backtest_runs(limit: int = 20, db: Session = DB_DEPENDENCY):
    """List recent backtest runs"""
    from ..models.backtest_v2 import BacktestRunV2

    runs = db.query(BacktestRunV2).order_by(
        BacktestRunV2.created_at.desc()
    ).limit(limit).all()

    return {
        "runs": [
            {
                "id": r.run_id,
                "name": f"{r.strategy} - {r.asset_type.upper()}",
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "total_return": r.total_return,
                "sharpe_ratio": r.sharpe_ratio,
                "max_drawdown": r.max_drawdown,
            }
            for r in runs
        ]
    }
