from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import or_
from datetime import datetime
from typing import List, Optional
from ..database import get_db
from ..models import IndexMembership, Company, HistoricalPrice
from ..services.market_data_service import MarketDataService
from ..services.live_market_service import live_market
from ..services.yfinance_service import yfinance_service

router = APIRouter(prefix="/market", tags=["Market Dashboard"])

@router.get("/overview")
def get_market_overview():
    """
    Returns a comprehensive market overview including:
    - Global Indices (US, India, etc.)
    - Commodities (Gold, Silver)
    - Market Sentiment (Fear & Greed, India VIX)
    - Technical Market Condition (Trend/Range)
    """
    try:
        # Fetch data in parallel conceptually (synchronous calls for now)
        indices = MarketDataService.get_global_indices()
        sentiment = MarketDataService.get_market_sentiment()
        condition = MarketDataService.get_market_condition()
        
        return {
            "indices": indices,
            "sentiment": sentiment,
            "condition": condition,
            "timestamp": "now"
        }
    except Exception as e:
        return {"error": str(e)}

@router.get("/sentiment")
def get_market_sentiment_endpoint(db: Session = Depends(get_db)):
    """
    Legacy/Specific sentiment endpoint if needed, or mapped to new service.
    """
    return MarketDataService.get_market_sentiment()

@router.get("/indices")
def get_index_performance():
    """
    Provide performance data for global market indices.
    
    Returns:
        dict: Performance information for global indices, typically including fields such as symbol, price, change_pct, and timestamp.
    """
    return MarketDataService.get_global_indices()

@router.get("/market-overview")
def get_market_overview_v2(db: Session = Depends(get_db)):
    """
    Assemble a market overview combining live data (when market is open), delayed yfinance data, and a database fallback.
    
    The returned dictionary contains:
    - market_status: current market status string from the live market service.
    - is_live: `True` when live market data was used or market is open, `False` otherwise.
    - indices: mapping of index symbol to its data. Each index entry contains:
        - price (float): latest available price.
        - change_pct (float): percentage change (0 if unavailable from fallback).
        - source (str): one of `"live"`, `"yfinance"`, or `"database"` indicating the data source.
        - date (str, optional): ISO-formatted date of the database price when the database was used.
    
    Returns:
        dict: Market overview as described above.
    """
    # Key indices to track
    indices = ['NIFTY50', 'BANKNIFTY']

    # Check if market is open
    is_market_open = live_market.is_market_open()

    data = {
        'market_status': live_market.get_status(),
        'is_live': is_market_open,
        'indices': {}
    }

    if is_market_open:
        # Try to get live data from cache
        for index in indices:
            tick = live_market.get_latest_tick(index)
            if tick:
                data['indices'][index] = {
                    'price': tick.get('ltp') or tick.get('price'),
                    'change_pct': tick.get('chp') or tick.get('change_pct'),
                    'source': 'live'
                }

    # Fallback for indices not found in live cache or if market is closed
    missing_indices = [idx for idx in indices if idx not in data['indices']]

    if missing_indices:
        # Try yfinance
        quotes = yfinance_service.get_quotes(missing_indices)
        for index, quote in quotes.items():
            if quote:
                data['indices'][index] = quote
            else:
                # Fallback to database
                latest_price = (
                    db.query(HistoricalPrice)
                    .join(Company)
                    .filter(Company.symbol == index)
                    .order_by(HistoricalPrice.date.desc())
                    .first()
                )
                if latest_price:
                    data['indices'][index] = {
                        'price': float(latest_price.close),
                        'change_pct': 0,
                        'source': 'database',
                        'date': latest_price.date.isoformat()
                    }

    return data

@router.get("/top-gainers")
def get_top_gainers(
    limit: int = 10,
    index: str = "NIFTY50",
    db: Session = Depends(get_db)
):
    """
    Return the top gaining constituents for a given index using live market data when available and falling back to delayed quotes.
    
    Parameters:
        limit (int): Maximum number of gainers to return.
        index (str): Index name whose constituents will be evaluated (e.g., "NIFTY50").
    
    Returns:
        dict: A dictionary containing:
            - data (list): List of gainers where each item contains `symbol`, `price`, `change_pct`, and `source` ('live' or 'yfinance').
            - is_live (bool): True if live market data was used (market open), False otherwise.
            - count (int): Number of gainers found.
            - message (str, optional): Present when no constituents are found for the given index.
    """
    current_date = datetime.now().date()

    # Query index constituents
    symbols_query = (
        db.query(IndexMembership.symbol)
        .filter(
            IndexMembership.index_name == index,
            IndexMembership.start_date <= current_date,
            or_(
                IndexMembership.end_date.is_(None),
                IndexMembership.end_date >= current_date
            )
        )
        .all()
    )
    symbols = [s[0] for s in symbols_query]

    if not symbols:
        return {"data": [], "is_live": False, "message": "No constituents found for index"}

    is_market_open = live_market.is_market_open()
    gainers = []

    if is_market_open:
        # Use live cache
        live_ticks = live_market.get_latest_ticks(symbols)
        for symbol, tick in live_ticks.items():
            change_pct = tick.get('chp') or tick.get('change_pct', 0)
            if change_pct > 0:
                gainers.append({
                    'symbol': symbol,
                    'price': tick.get('ltp') or tick.get('price'),
                    'change_pct': change_pct,
                    'source': 'live'
                })

    # If no live gainers (e.g. market just opened or closed), use yfinance
    if not gainers:
        # Limit to 50 symbols to avoid yfinance rate limits
        quotes = yfinance_service.get_quotes(symbols[:50])
        for symbol, quote in quotes.items():
            if quote and quote.get('change_pct', 0) > 0:
                gainers.append({
                    'symbol': symbol,
                    'price': quote['price'],
                    'change_pct': quote['change_pct'],
                    'source': 'yfinance'
                })

    # Sort and limit
    gainers.sort(key=lambda x: x['change_pct'], reverse=True)

    return {
        'data': gainers[:limit],
        'is_live': is_market_open,
        'count': len(gainers)
    }