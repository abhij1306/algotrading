# Market Data Router
# Provides clean API endpoints for market data access

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Dict, Any
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent.parent.parent))

from app.database import get_db, Company
from app.utils.market_hours import is_market_open, get_market_status
from ..services.nse_data_reader import NSEDataReader
from ..services.fyers_direct import get_fyers_quotes

router = APIRouter(prefix="/api/market", tags=["market-data"])

# Initialize NSE reader
nse_reader = NSEDataReader()

@router.get("/status")
def market_status():
    """Get current market status (open/closed)"""
    return get_market_status()

@router.get("/overview")
async def get_market_overview(db: Session = Depends(get_db)):
    """
    Get comprehensive market overview for dashboard
    Returns: indices, sentiment, market condition
    """
    try:
        # This will be implemented to aggregate data from various sources
        # For now, return a basic structure
        return {
            "indices": [],
            "sentiment": {
                "us_fear_greed": {"score": 50, "status": "Neutral"},
                "india_sentiment": {"score": 50, "status": "Neutral"}
            },
            "condition": {"status": "Unknown", "adx": 0},
            "timestamp": ""
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/search")
def search_symbols(
    query: str, 
    exclude_indices: bool = False, 
    db: Session = Depends(get_db)
):
    """
    Search for companies and indices by symbol or name
    exclude_indices: Set to True to only return equities (for analyst mode)
    """
    if not query or len(query) < 2:
        return []
    
    from app.constants.indices import STOCK_INDICES
    
    results_list = []
    
    # Search in indices first (skip if exclude_indices=True)
    if not exclude_indices:
        for idx_key, idx_info in STOCK_INDICES.items():
            idx_name = idx_info.get("name", "")
            if (query.upper() in idx_key.upper() or 
                query.upper() in idx_name.upper()):
                results_list.append({
                    "symbol": idx_key,
                    "name": idx_name,
                    "sector": "INDEX",
                    "type": "INDEX"
                })
    
    # Search in Company table (equities only)
    companies = db.query(Company).filter(
        (Company.symbol.ilike(f"%{query}%")) | 
        (Company.name.ilike(f"%{query}%"))
    ).limit(10).all()
    
    for c in companies:
        results_list.append({
            "symbol": c.symbol,
            "name": c.name,
            "sector": c.sector,
            "type": "EQUITY"
        })
    
    return results_list[:15]

@router.get("/quote/{symbol}")
async def get_quote(symbol: str, db: Session = Depends(get_db)):
    """Get latest quote for a symbol"""
    try:
        # Check if market is open for live data
        is_open, _ = is_market_open()
        
        if is_open:
            # Try to get live quote from Fyers
            try:
                quotes = get_fyers_quotes([symbol])
                if symbol in quotes:
                    return quotes[symbol]
            except:
                pass
        
        # Fallback to historical data
        company = db.query(Company).filter(Company.symbol == symbol).first()
        if not company:
            raise HTTPException(status_code=404, detail="Symbol not found")
        
        # Return basic company info
        return {
            "symbol": company.symbol,
            "name": company.name,
            "ltp": 0,  # Will be populated from historical or live data
            "change": 0,
            "change_pct": 0
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/indices")
def get_indices():
    """Get list of available indices"""
    from app.constants.indices import STOCK_INDICES
    
    indices = []
    for key, info in STOCK_INDICES.items():
        indices.append({
            "id": key,
            "name": info.get("name", key),
            "symbol": info.get("symbol", key)
        })
    
    return {"indices": indices, "default": "NIFTY50"}

@router.get("/sectors")
def get_sectors(db: Session = Depends(get_db)):
    """Get list of all available sectors"""
    sectors = db.query(Company.sector).distinct()\
        .filter(Company.sector != None)\
        .order_by(Company.sector).all()
    
    return {"sectors": [s[0] for s in sectors]}

@router.get("/quotes/live")
def get_live_quotes(symbols: str, db: Session = Depends(get_db)):
    """
    Get live quotes for multiple symbols (only during market hours)
    symbols: comma-separated list (e.g., "RELIANCE,TCS,INFY")
    """
    is_open, message = is_market_open()
    if not is_open:
        raise HTTPException(
            status_code=503,
            detail=f"Live quotes unavailable: {message}"
        )
    
    try:
        symbol_list = [s.strip() for s in symbols.split(',')]
        quotes = get_fyers_quotes(symbol_list)
        return {"quotes": quotes}
    except Exception as e:
        return {"quotes": {}, "error": str(e)}
