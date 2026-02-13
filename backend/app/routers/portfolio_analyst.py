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
    """
    Create and persist a new user stock portfolio from the provided positions.
    
    Creates a UserPortfolio and associated PortfolioPosition records linked to existing Company entries, computes each position's allocation percentage relative to the total invested value, and commits them to the database.
    
    Returns:
        dict: Contains the new portfolio's `id` and a success `message`.
    
    Raises:
        HTTPException: 404 if any provided symbol is not found.
        HTTPException: 500 if a database error or other server-side error occurs.
    """
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
    """
    Return a list of all user stock portfolios with basic metadata.
    
    Returns:
        dict: A mapping with key "portfolios" containing a list of portfolio summaries. Each summary is a dict with:
            - id (int): Portfolio database identifier.
            - portfolio_name (str): Name of the portfolio.
            - description (str | None): Optional portfolio description.
            - created_at (str | None): ISO 8601 timestamp string of creation time, or None if not set.
            - num_positions (int): Number of positions associated with the portfolio.
    """
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
    """
    Retrieve detailed information for a specific stock portfolio.
    
    Parameters:
        portfolio_id (int): ID of the portfolio to fetch.
    
    Returns:
        dict: Portfolio details with keys:
            - id (int): Portfolio ID.
            - portfolio_name (str): Portfolio name.
            - description (str | None): Portfolio description.
            - positions (list[dict]): List of positions, each containing:
                - symbol (str)
                - company_name (str)
                - invested_value (float)
                - quantity (float | None)
                - avg_buy_price (float | None)
                - allocation_pct (float)
            - total_invested (float): Sum of invested_value for all positions.
    
    Raises:
        HTTPException: 404 if the portfolio is not found.
    """
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
    """
    Delete the user stock portfolio identified by `portfolio_id`.
    
    Parameters:
        portfolio_id (int): ID of the portfolio to delete.
    
    Raises:
        HTTPException: 404 if no portfolio exists with the given `portfolio_id`.
    """
    portfolio = db.query(UserPortfolio).filter(UserPortfolio.id == portfolio_id).first()
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    db.delete(portfolio)
    db.commit()

@router.post("/{portfolio_id}/analyze")
async def analyze_stock_portfolio(portfolio_id: int, lookback_days: int = 252, db: Session = Depends(get_db)):
    """
    Run a risk analysis for the specified stock portfolio and return analysis results including market risk metrics and chart data.
    
    Parameters:
        portfolio_id (int): ID of the portfolio to analyze.
        lookback_days (int): Number of historical days to use for price and risk calculations (default 252).
    
    Returns:
        dict: Analysis result containing computed risk metrics (including a 'market_risk' mapping), chart-ready data under 'charts' (performance dates, portfolio and benchmark cumulative returns, and sector allocations), and other engine-produced analysis fields.
    
    Raises:
        HTTPException: 404 if the portfolio does not exist.
        HTTPException: 400 if the portfolio has no positions or if no historical price data is available for any position.
    """
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