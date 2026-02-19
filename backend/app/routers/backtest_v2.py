"""
Backtest V2 Router
==================
Enhanced API endpoints for backtesting with comprehensive tracking.
"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..database import get_db
from ..engines.backtest_engine import BacktestConfig, run_backtest
from ..models.backtest_v2 import (
    BacktestComparison,
    BacktestEquityPoint,
    BacktestMonthlyReturn,
    BacktestRunV2,
    BacktestTrade,
    SavedBacktestConfig,
)

router = APIRouter(prefix="/v2", tags=["backtest-v2"])
DB_DEPENDENCY = Depends(get_db)


# ============================================================================
# Request/Response Models
# ============================================================================

class CostsConfig(BaseModel):
    brokerage: float = 0.001
    slippage: float = 0.0005
    stamp_duty: float = 0.0002


class StockConfig(BaseModel):
    symbols: list[str]
    position_sizing: dict = Field(default_factory=lambda: {"type": "percent_of_equity", "value": 100})
    max_positions: int = 10
    long_short: str = "long"


class OptionConfig(BaseModel):
    underlying: str
    option_type: str = "CE"  # CE, PE, both
    strike_selection: str = "ATM"
    expiry_selection: str = "weekly"
    roll_strategy: str = "at_expiry"


class IndexConfig(BaseModel):
    universe: str = "NIFTY50"
    reconstruction: bool = True
    selection_criteria: dict = Field(default_factory=lambda: {"type": "top_n", "metric": "momentum", "n": 10})
    rebalancing: dict = Field(default_factory=lambda: {"frequency": "monthly", "day_of_month": 1})


class BacktestRunRequest(BaseModel):
    name: str | None = None
    asset_type: str = Field(..., description="stock, option, or index")
    strategy: str = Field(..., description="Strategy identifier")
    date_range: dict = Field(..., description="{start: YYYY-MM-DD, end: YYYY-MM-DD}")
    initial_capital: float = 1000000.0
    costs: CostsConfig = Field(default_factory=CostsConfig)

    # Asset-specific config
    stock_config: StockConfig | None = None
    option_config: OptionConfig | None = None
    index_config: IndexConfig | None = None

    # Advanced options
    use_mock_data: bool = False
    monte_carlo_sims: int = 1000
    run_walk_forward: bool = False


class BacktestRunResponse(BaseModel):
    success: bool
    run_id: str | None = None
    estimated_time: int = 30  # seconds
    error: str | None = None


class BacktestListItem(BaseModel):
    run_id: str
    name: str | None
    asset_type: str
    strategy: str
    start_date: str
    end_date: str
    initial_capital: float
    final_capital: float | None
    total_return: float | None
    sharpe_ratio: float | None
    max_drawdown: float | None
    status: str
    created_at: str


class BacktestDetailResponse(BaseModel):
    run_id: str
    config: dict
    status: str

    # Performance
    metrics: dict
    stats: dict | None = None

    # Data
    equity_curve: list[dict]
    trades: list[dict]
    monthly_returns: list[dict]

    # Advanced analysis
    monte_carlo: dict | None = None
    walk_forward: dict | None = None
    benchmark_comparison: dict | None = None


# ============================================================================
# Run Backtest
# ============================================================================

@router.post("/run", response_model=BacktestRunResponse)
async def run_backtest_v2(request: BacktestRunRequest, db: Session = DB_DEPENDENCY):
    """Run a new backtest with comprehensive tracking"""
    try:
        run_id = f"BT-{uuid.uuid4().hex[:8].upper()}"

        # Create run record
        run = BacktestRunV2(
            run_id=run_id,
            config=request.model_dump(),
            asset_type=request.asset_type,
            strategy=request.strategy,
            start_date=datetime.strptime(request.date_range["start"], "%Y-%m-%d").date(),
            end_date=datetime.strptime(request.date_range["end"], "%Y-%m-%d").date(),
            initial_capital=request.initial_capital,
            status="running"
        )
        db.add(run)
        db.commit()

        # TODO(#3): Run actual backtest asynchronously
        # For now, return immediately with run_id
        # The actual computation would be done by a background worker

        return BacktestRunResponse(
            success=True,
            run_id=run_id,
            estimated_time=30
        )

    except Exception as e:
        logger.error(f"Error running backtest: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to run backtest: {str(e)}"
        )


@router.post("/run-sync", response_model=dict)
async def run_backtest_sync(request: BacktestRunRequest, db: Session = DB_DEPENDENCY):
    """Run backtest synchronously (for development/testing)"""
    try:
        run_id = f"BT-{uuid.uuid4().hex[:8].upper()}"

        # Map to existing backtest engine config
        config = BacktestConfig(
            start_date=datetime.strptime(request.date_range["start"], "%Y-%m-%d").date(),
            end_date=datetime.strptime(request.date_range["end"], "%Y-%m-%d").date(),
            initial_capital=request.initial_capital,
            universe=request.index_config.universe if request.index_config else "NIFTY50",
            strategy=request.strategy,
            brokerage=request.costs.brokerage,
            slippage=request.costs.slippage,
        )

        # Run backtest
        result = run_backtest(config, save_to_db=False)

        # Save to V2 tables
        run = BacktestRunV2(
            run_id=run_id,
            config=request.model_dump(),
            asset_type=request.asset_type,
            strategy=request.strategy,
            start_date=config.start_date,
            end_date=config.end_date,
            initial_capital=config.initial_capital,
            final_capital=result.metrics.get("final_equity"),
            status="completed",
            completed_at=datetime.utcnow(),
            **{k: v for k, v in result.metrics.items() if hasattr(BacktestRunV2, k)}
        )
        db.add(run)

        # Save equity curve
        for date, equity in result.equity_curve:
            point = BacktestEquityPoint(
                run_id=run_id,
                date=date,
                equity=equity,
                cash=equity * 0.1,  # Approximate
                positions_value=equity * 0.9
            )
            db.add(point)

        # Save trades
        for i, trade in enumerate(result.trades):
            t = BacktestTrade(
                run_id=run_id,
                trade_id=f"T-{i+1}",
                symbol=trade.get("symbol", "UNKNOWN"),
                entry_date=datetime.strptime(trade.get("date", request.date_range["start"]), "%Y-%m-%d").date(),
                entry_price=trade.get("price", 0),
                quantity=trade.get("quantity", 0),
                pnl=trade.get("pnl"),
                return_pct=trade.get("return_pct")
            )
            db.add(t)

        db.commit()

        return {
            "success": True,
            "run_id": run_id,
            "metrics": result.metrics
        }

    except Exception as e:
        logger.error(f"Error running backtest sync: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to run backtest: {str(e)}"
        )


# ============================================================================
# Get Results
# ============================================================================

@router.get("/results/{run_id}", response_model=BacktestDetailResponse)
async def get_backtest_results(run_id: str, db: Session = DB_DEPENDENCY):
    """Get complete backtest results"""
    run = db.query(BacktestRunV2).filter(BacktestRunV2.run_id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Backtest run not found")

    # Get equity curve
    equity_points = db.query(BacktestEquityPoint).filter(
        BacktestEquityPoint.run_id == run_id
    ).order_by(BacktestEquityPoint.date).all()

    # Get trades
    trades = db.query(BacktestTrade).filter(
        BacktestTrade.run_id == run_id
    ).order_by(BacktestTrade.entry_date).all()

    # Get monthly returns
    monthly = db.query(BacktestMonthlyReturn).filter(
        BacktestMonthlyReturn.run_id == run_id
    ).order_by(BacktestMonthlyReturn.year, BacktestMonthlyReturn.month).all()

    return BacktestDetailResponse(
        run_id=run.run_id,
        config=run.config,
        status=run.status,
        metrics={
            "total_return": run.total_return,
            "cagr": run.cagr,
            "sharpe_ratio": run.sharpe_ratio,
            "sortino_ratio": run.sortino_ratio,
            "max_drawdown": run.max_drawdown,
            "max_drawdown_duration": run.max_drawdown_duration,
            "calmar_ratio": run.calmar_ratio,
            "win_rate": run.win_rate,
            "profit_factor": run.profit_factor,
            "total_trades": run.total_trades,
            "winning_trades": run.winning_trades,
            "losing_trades": run.losing_trades,
        },
        stats={
            "avg_trade_return": run.avg_trade_return,
            "avg_win": run.avg_win,
            "avg_loss": run.avg_loss,
            "largest_win": run.largest_win,
            "largest_loss": run.largest_loss,
            "avg_trade_duration": run.avg_trade_duration,
        } if run.status == "completed" else None,
        equity_curve=[
            {
                "date": p.date.isoformat(),
                "equity": p.equity,
                "cash": p.cash,
                "positions_value": p.positions_value,
                "drawdown": p.drawdown
            }
            for p in equity_points
        ],
        trades=[
            {
                "id": t.trade_id,
                "symbol": t.symbol,
                "entry_date": t.entry_date.isoformat() if t.entry_date else None,
                "exit_date": t.exit_date.isoformat() if t.exit_date else None,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "quantity": t.quantity,
                "pnl": t.pnl,
                "return": t.return_pct,
                "duration": t.duration_days
            }
            for t in trades
        ],
        monthly_returns=[
            {
                "year": m.year,
                "month": m.month,
                "return": m.return_pct
            }
            for m in monthly
        ],
        monte_carlo=None,  # TODO(#4): Generate on demand
        walk_forward=None,  # TODO(#5): Generate on demand
        benchmark_comparison=None  # TODO(#6): Add benchmark
    )


# ============================================================================
# List Runs
# ============================================================================

@router.get("/runs", response_model=list[BacktestListItem])
async def list_backtest_runs(
    asset_type: str | None = None,
    strategy: str | None = None,
    status: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = DB_DEPENDENCY
):
    """List backtest runs with filtering"""
    query = db.query(BacktestRunV2)

    if asset_type:
        query = query.filter(BacktestRunV2.asset_type == asset_type)
    if strategy:
        query = query.filter(BacktestRunV2.strategy == strategy)
    if status:
        query = query.filter(BacktestRunV2.status == status)

    runs = query.order_by(desc(BacktestRunV2.created_at)).offset(offset).limit(limit).all()

    return [
        BacktestListItem(
            run_id=r.run_id,
            name=r.config.get("name") if r.config else None,
            asset_type=r.asset_type,
            strategy=r.strategy,
            start_date=r.start_date.isoformat(),
            end_date=r.end_date.isoformat(),
            initial_capital=r.initial_capital,
            final_capital=r.final_capital,
            total_return=r.total_return,
            sharpe_ratio=r.sharpe_ratio,
            max_drawdown=r.max_drawdown,
            status=r.status,
            created_at=r.created_at.isoformat() if r.created_at else None
        )
        for r in runs
    ]


# ============================================================================
# Delete Run
# ============================================================================

@router.delete("/runs/{run_id}")
async def delete_backtest_run(run_id: str, db: Session = DB_DEPENDENCY):
    """Delete a backtest run and all associated data"""
    run = db.query(BacktestRunV2).filter(BacktestRunV2.run_id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Backtest run not found")

    db.delete(run)
    db.commit()

    return {"success": True, "message": f"Backtest {run_id} deleted"}


# ============================================================================
# Saved Configs
# ============================================================================

@router.post("/configs")
async def save_config(
    name: str,
    config: dict,
    asset_type: str,
    strategy: str,
    description: str | None = None,
    tags: list[str] | None = None,
    db: Session = DB_DEPENDENCY
):
    """Save a backtest configuration for later use"""
    saved = SavedBacktestConfig(
        name=name,
        description=description,
        config=config,
        asset_type=asset_type,
        strategy=strategy,
        tags=tags or []
    )
    db.add(saved)
    db.commit()

    return {"success": True, "id": saved.id}


@router.get("/configs")
async def list_saved_configs(
    asset_type: str | None = None,
    db: Session = DB_DEPENDENCY
):
    """List saved backtest configurations"""
    query = db.query(SavedBacktestConfig)
    if asset_type:
        query = query.filter(SavedBacktestConfig.asset_type == asset_type)

    configs = query.order_by(desc(SavedBacktestConfig.created_at)).all()

    return [
        {
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "asset_type": c.asset_type,
            "strategy": c.strategy,
            "tags": c.tags,
            "created_at": c.created_at.isoformat() if c.created_at else None
        }
        for c in configs
    ]


# ============================================================================
# Comparisons
# ============================================================================

@router.post("/comparisons")
async def create_comparison(
    name: str,
    run_ids: list[str],
    description: str | None = None,
    db: Session = DB_DEPENDENCY
):
    """Create a comparison of multiple backtest runs"""
    # Verify all runs exist
    runs = db.query(BacktestRunV2).filter(BacktestRunV2.run_id.in_(run_ids)).all()
    if len(runs) != len(run_ids):
        found_ids = {r.run_id for r in runs}
        missing = set(run_ids) - found_ids
        raise HTTPException(status_code=404, detail=f"Runs not found: {missing}")

    # Calculate comparison metrics
    comparison_metrics = {
        "runs": [
            {
                "run_id": r.run_id,
                "strategy": r.strategy,
                "total_return": r.total_return,
                "sharpe_ratio": r.sharpe_ratio,
                "max_drawdown": r.max_drawdown,
            }
            for r in runs
        ],
        "best_return": max((r.total_return for r in runs if r.total_return is not None), default=None),
        "best_sharpe": max((r.sharpe_ratio for r in runs if r.sharpe_ratio is not None), default=None),
        "lowest_drawdown": min((r.max_drawdown for r in runs if r.max_drawdown is not None), default=None),
    }

    comparison = BacktestComparison(
        name=name,
        description=description,
        run_ids=run_ids,
        comparison_metrics=comparison_metrics
    )
    db.add(comparison)
    db.commit()

    return {
        "success": True,
        "id": comparison.id,
        "comparison": comparison_metrics
    }


@router.get("/comparisons/{comparison_id}")
async def get_comparison(comparison_id: int, db: Session = DB_DEPENDENCY):
    """Get comparison details"""
    comparison = db.query(BacktestComparison).filter(BacktestComparison.id == comparison_id).first()
    if not comparison:
        raise HTTPException(status_code=404, detail="Comparison not found")

    # Get full run details
    runs = db.query(BacktestRunV2).filter(
        BacktestRunV2.run_id.in_(comparison.run_ids)
    ).all()

    return {
        "id": comparison.id,
        "name": comparison.name,
        "description": comparison.description,
        "runs": [
            {
                "run_id": r.run_id,
                "asset_type": r.asset_type,
                "strategy": r.strategy,
                "metrics": {
                    "total_return": r.total_return,
                    "cagr": r.cagr,
                    "sharpe_ratio": r.sharpe_ratio,
                    "max_drawdown": r.max_drawdown,
                    "win_rate": r.win_rate,
                    "profit_factor": r.profit_factor,
                }
            }
            for r in runs
        ],
        "comparison": comparison.comparison_metrics,
        "created_at": comparison.created_at.isoformat() if comparison.created_at else None
    }
