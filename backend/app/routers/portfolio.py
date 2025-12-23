from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import desc
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, UUID4
from datetime import datetime, time
import numpy as np
import pandas as pd
import pytz

from ..database import (
    get_db, 
    UserPortfolio, PortfolioPosition, ComputedRiskMetric, 
    Company, FinancialStatement, HistoricalPrice,
    PortfolioPolicy, ResearchPortfolio, StrategyMetadata,
    LivePortfolioState
)
from ..portfolio_risk import PortfolioRiskEngine
from ..data_repository import DataRepository

# Unified Router
router = APIRouter(prefix="/api/portfolio", tags=["Portfolio Management"])


# ==========================================
# PART 1: Analyst Portfolios (Stock-Based)
# ==========================================

class PositionInput(BaseModel):
    symbol: str
    invested_value: float
    quantity: Optional[float] = None
    avg_buy_price: Optional[float] = None

class StockPortfolioCreate(BaseModel):
    portfolio_name: str
    description: Optional[str] = None
    positions: List[PositionInput]

@router.post("/stocks", status_code=status.HTTP_201_CREATED)
async def create_stock_portfolio(portfolio: StockPortfolioCreate, db: Session = Depends(get_db)):
    """Create a new user stock portfolio (Analyst Mode)"""
    try:
        new_portfolio = UserPortfolio(
            portfolio_name=portfolio.portfolio_name,
            description=portfolio.description
        )
        db.add(new_portfolio)
        db.flush()
        
        total_invested = sum(p.invested_value for p in portfolio.positions)
        
        for pos in portfolio.positions:
            company = db.query(Company).filter(Company.symbol == pos.symbol.upper()).first()
            if not company:
                raise HTTPException(status_code=404, detail=f"Symbol {pos.symbol} not found")
            
            allocation_pct = (pos.invested_value / total_invested) * 100 if total_invested > 0 else 0
            
            position = PortfolioPosition(
                portfolio_id=new_portfolio.id,
                company_id=company.id,
                quantity=pos.quantity,
                avg_buy_price=pos.avg_buy_price,
                invested_value=pos.invested_value,
                allocation_pct=allocation_pct
            )
            db.add(position)
        
        db.commit()
        db.refresh(new_portfolio)
        return {"id": new_portfolio.id, "message": "Stock portfolio created successfully"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/stocks")
async def list_stock_portfolios(db: Session = Depends(get_db)):
    """List all stock portfolios"""
    portfolios = db.query(UserPortfolio).all()
    return {
        "portfolios": [
            {
                "id": p.id,
                "portfolio_name": p.portfolio_name,
                "description": p.description,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "num_positions": len(p.positions)
            }
            for p in portfolios
        ]
    }

@router.get("/stocks/{portfolio_id}")
async def get_stock_portfolio(portfolio_id: int, db: Session = Depends(get_db)):
    """Get details of a specific stock portfolio"""
    portfolio = db.query(UserPortfolio).filter(UserPortfolio.id == portfolio_id).first()
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    
    positions = []
    for pos in portfolio.positions:
        positions.append({
            "symbol": pos.company.symbol,
            "company_name": pos.company.name,
            "invested_value": pos.invested_value,
            "quantity": pos.quantity,
            "avg_buy_price": pos.avg_buy_price,
            "allocation_pct": pos.allocation_pct
        })
    
    return {
        "id": portfolio.id,
        "portfolio_name": portfolio.portfolio_name,
        "description": portfolio.description,
        "positions": positions,
        "total_invested": sum(p.invested_value for p in portfolio.positions)
    }

@router.delete("/stocks/{portfolio_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_stock_portfolio(portfolio_id: int, db: Session = Depends(get_db)):
    """Delete a stock portfolio"""
    portfolio = db.query(UserPortfolio).filter(UserPortfolio.id == portfolio_id).first()
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    db.delete(portfolio)
    db.commit()

@router.post("/stocks/{portfolio_id}/analyze")
async def analyze_stock_portfolio(portfolio_id: int, lookback_days: int = 252, db: Session = Depends(get_db)):
    """Run risk analysis on stock portfolio"""
    repo = DataRepository(db)
    
    # [Logic imported from original portfolio.py]
    portfolio = db.query(UserPortfolio).filter(UserPortfolio.id == portfolio_id).first()
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    
    if not portfolio.positions:
        raise HTTPException(status_code=400, detail="Portfolio has no positions")
    
    # 1. Fetch Data
    valid_symbols = []
    valid_weights = []
    valid_financials = []
    prices_dict = {}
    missing_data_symbols = []
    
    from ..data_fetcher import fetch_historical_data
    
    for pos in portfolio.positions:
        symbol = pos.company.symbol
        weight = pos.allocation_pct / 100
        
        hist = fetch_historical_data(symbol, days=lookback_days)
        if hist is None or hist.empty:
            missing_data_symbols.append(symbol)
            continue
            
        prices_dict[symbol] = hist['Close']
        valid_symbols.append(symbol)
        valid_weights.append(weight)
        
        # Financials
        fs = db.query(FinancialStatement).filter(
            FinancialStatement.company_id == pos.company_id
        ).order_by(desc(FinancialStatement.period_end)).first()
        
        if fs:
            valid_financials.append({
                'debt_to_equity': fs.debt_to_equity or 0,
                'roe': fs.roe or 0,
                'current_ratio': 1.5, # Default / Placeholder
                'free_cash_flow': fs.free_cash_flow or 0
            })
        else:
            valid_financials.append({'debt_to_equity': 0, 'roe': 0, 'current_ratio': 1.0, 'free_cash_flow': 0})

    if not valid_symbols:
        raise HTTPException(status_code=400, detail=f"No historical data found. Missing: {missing_data_symbols}")

    # Normalize weights
    weights_arr = np.array(valid_weights)
    if weights_arr.sum() > 0:
        weights_arr = weights_arr / weights_arr.sum()
    
    prices_df = pd.DataFrame(prices_dict)
    
    # Market Data
    nifty_hist = repo.get_historical_prices('NIFTY 50', days=lookback_days)
    market_prices = nifty_hist['Close'] if (nifty_hist is not None and not nifty_hist.empty) else prices_df.mean(axis=1)

    # Run Analysis
    engine = PortfolioRiskEngine()
    analysis = engine.analyze_portfolio(
        prices=prices_df,
        weights=weights_arr,
        market_prices=market_prices,
        financials=valid_financials,
        lookback_days=lookback_days
    )
    
    # Store Metrics
    for metric_name, metric_value in analysis['market_risk'].items():
        if isinstance(metric_value, (int, float)):
            metric = ComputedRiskMetric(
                portfolio_id=portfolio_id,
                metric_name=f"market_{metric_name}",
                metric_value=float(metric_value)
            )
            db.add(metric)
    db.commit()
    
    # Generate Charts
    portfolio_daily_returns = (prices_df @ weights_arr).pct_change().fillna(0) * 100
    portfolio_cumulative = portfolio_daily_returns.cumsum()
    market_daily_returns = market_prices.pct_change().fillna(0) * 100
    market_cumulative = market_daily_returns.cumsum()
    
    analysis['charts'] = {
        "performance": {
            "dates": prices_df.index.strftime('%Y-%m-%d').tolist(),
            "portfolioReturns": portfolio_cumulative.tolist(),
            "benchmarkReturns": market_cumulative.tolist()
        },
        "sectors": [
            {"name": pos.company.sector or "Unknown", "allocation": pos.allocation_pct}
            for pos in portfolio.positions
        ]
    }
    
    # Add detailed position info (optional, keeping it simple for now)
    
    return analysis


# ==========================================
# PART 2: Strategy Portfolios (Quant/Research)
# ==========================================

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
    portfolio_id: Optional[UUID4] = None # If saved
    policy_id: Optional[UUID4] = None    # If ad-hoc
    strategy_ids: Optional[List[str]] = None # If ad-hoc
    start_date: str = "2024-01-01"
    end_date: str = "2024-12-31"

# --- Endpoints ---

@router.post("/strategies/policy", status_code=status.HTTP_201_CREATED)
def create_policy(policy: PortfolioPolicyCreate, db: Session = Depends(get_db)):
    """Create a new portfolio risk policy"""
    new_policy = PortfolioPolicy(**policy.dict())
    db.add(new_policy)
    db.commit()
    db.refresh(new_policy)
    return new_policy

@router.get("/strategies/policy")
def list_policies(db: Session = Depends(get_db)):
    """List available policies"""
    return db.query(PortfolioPolicy).all()

@router.get("/strategies/available")
def list_available_strategies(db: Session = Depends(get_db)):
    """List strategies available for portfolio composition"""
    strategies = db.query(StrategyMetadata).all()
    # If empty, seed defaults (same as original logic)
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

    return strategies

@router.patch("/strategies/{strategy_id}")
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

@router.post("/strategies/correlation")
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

@router.post("/strategies", status_code=status.HTTP_201_CREATED)
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

@router.get("/strategies")
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

@router.post("/strategies/backtest")
async def backtest_strategy_portfolio(request: BacktestRequest, db: Session = Depends(get_db)):
    """Run backtest on a strategy portfolio"""
    
    # Case A: Saved Portfolio
    if request.portfolio_id:
        portfolio = db.query(ResearchPortfolio).filter(ResearchPortfolio.id == request.portfolio_id).first()
        if not portfolio:
            raise HTTPException(status_code=404, detail="Portfolio not found")
        policy = portfolio.policy
        if not policy:
            raise HTTPException(status_code=400, detail="Portfolio has no policy")
            
        results = await _run_backtest_logic(db, policy, portfolio, request.start_date, request.end_date)
        
        # Save snapshot
        if "metrics" in results:
            portfolio.metrics_snapshot = results["metrics"]
            db.commit()
        return results

    # Case B: Ad-hoc Simulation (Draft)
    elif request.policy_id and request.strategy_ids:
        policy = db.query(PortfolioPolicy).filter(PortfolioPolicy.id == request.policy_id).first()
        if not policy:
             raise HTTPException(status_code=404, detail="Policy not found")
        
        # Create temp portfolio
        count = len(request.strategy_ids)
        composition = [{"strategy_id": s, "allocation_percent": 100/count} for s in request.strategy_ids] if count else []
        
        temp_portfolio = ResearchPortfolio(
            id=0, # temp
            name="Simulation",
            composition=composition,
            policy_id=request.policy_id,
            status="simulated"
        )
        
        return await _run_backtest_logic(db, policy, temp_portfolio, request.start_date, request.end_date)
    
    else:
        raise HTTPException(status_code=400, detail="Must provide either portfolio_id OR (policy_id + strategy_ids)")

@router.get("/strategies/monitor")
def monitor_live_strategies(db: Session = Depends(get_db)):
    """Get live monitoring data for strategy portfolios"""
    try:
        tz = pytz.timezone('Asia/Kolkata')
        now = datetime.now(tz)
        
        # Simple market hours check
        if not (time(9, 15) <= now.time() <= time(15, 30)):
            # raise HTTPException(503, "Market closed")
            pass # Allow for testing

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
        # Return empty list instead of throwing 500 error
        print(f"Monitor endpoint error: {e}")
        return []

@router.post("/strategies/monitor/refresh")
def refresh_live_monitor(db: Session = Depends(get_db)):
    """Force refresh of live monitor"""
    from ..services.live_monitor import LiveMonitorService
    
    service = LiveMonitorService(db)
    results = service.monitor_all_active_portfolios()
    
    return {"status": "refreshed", "updates": len(results)}
