from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import List, Optional
from pydantic import BaseModel
import numpy as np
import pandas as pd

from ..database import (
    get_db,
    UserPortfolio, PortfolioPosition, ComputedRiskMetric,
    Company, FinancialStatement
)
from ..portfolio_risk import PortfolioRiskEngine
from ..data_repository import DataRepository

router = APIRouter(prefix="/api/portfolio/stocks", tags=["Analyst Portfolio"])

# --- Schemas ---

class PositionInput(BaseModel):
    symbol: str
    invested_value: float
    quantity: Optional[float] = None
    avg_buy_price: Optional[float] = None

class StockPortfolioCreate(BaseModel):
    portfolio_name: str
    description: Optional[str] = None
    positions: List[PositionInput]

# --- Endpoints ---

@router.post("", status_code=status.HTTP_201_CREATED)
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

@router.get("")
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

@router.get("/{portfolio_id}")
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

@router.delete("/{portfolio_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_stock_portfolio(portfolio_id: int, db: Session = Depends(get_db)):
    """Delete a stock portfolio"""
    portfolio = db.query(UserPortfolio).filter(UserPortfolio.id == portfolio_id).first()
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    db.delete(portfolio)
    db.commit()

@router.post("/{portfolio_id}/analyze")
async def analyze_stock_portfolio(portfolio_id: int, lookback_days: int = 252, db: Session = Depends(get_db)):
    """Run risk analysis on stock portfolio"""
    repo = DataRepository(db)

    portfolio = db.query(UserPortfolio).filter(UserPortfolio.id == portfolio_id).first()
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    if not portfolio.positions:
        raise HTTPException(status_code=400, detail="Portfolio has no positions")

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
                'current_ratio': 1.5,
                'free_cash_flow': fs.free_cash_flow or 0
            })
        else:
            valid_financials.append({'debt_to_equity': 0, 'roe': 0, 'current_ratio': 1.0, 'free_cash_flow': 0})

    if not valid_symbols:
        raise HTTPException(status_code=400, detail=f"No historical data found. Missing: {missing_data_symbols}")

    weights_arr = np.array(valid_weights)
    if weights_arr.sum() > 0:
        weights_arr = weights_arr / weights_arr.sum()

    prices_df = pd.DataFrame(prices_dict)

    nifty_hist = repo.get_historical_prices('NIFTY 50', days=lookback_days)
    market_prices = nifty_hist['Close'] if (nifty_hist is not None and not nifty_hist.empty) else prices_df.mean(axis=1)

    engine = PortfolioRiskEngine()
    analysis = engine.analyze_portfolio(
        prices=prices_df,
        weights=weights_arr,
        market_prices=market_prices,
        financials=valid_financials,
        lookback_days=lookback_days
    )

    # Clear old metrics before storing new ones
    db.query(ComputedRiskMetric).filter(
        ComputedRiskMetric.portfolio_id == portfolio_id
    ).delete()

    for metric_name, metric_value in analysis['market_risk'].items():
        if isinstance(metric_value, (int, float)):
            metric = ComputedRiskMetric(
                portfolio_id=portfolio_id,
                metric_name=f"market_{metric_name}",
                metric_value=float(metric_value)
            )
            db.add(metric)
    db.commit()

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

    return analysis
