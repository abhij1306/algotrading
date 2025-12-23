from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from ..database import get_db
from ..utils.market_hours import is_market_open, get_market_status
from typing import Dict, Any, List

router = APIRouter()

@router.get("/market/status")
def market_status():
    """Get current market status (open/closed)"""
    return get_market_status()

@router.get("/quotes/live")
def get_live_quotes(symbols: str, db: Session = Depends(get_db)):
    """
    Get live quotes for multiple symbols (only during market hours)
    symbols: comma-separated list (e.g., "RELIANCE,TCS,INFY")
    """
    # Check market hours
    is_open, message = is_market_open()
    if not is_open:
        raise HTTPException(
            status_code=503,
            detail=f"Live quotes unavailable: {message}"
        )
    
    try:
        symbol_list = [s.strip() for s in symbols.split(',')]
        
        # Fetch live quotes from Fyers
        try:
            from ..fyers_direct import get_fyers_quotes
            quotes = get_fyers_quotes(symbol_list)
            return {"quotes": quotes}
        except ImportError:
            print("Warning: fyers_direct module not found")
            return {"quotes": {}}
            
    except Exception as e:
        print(f"Error fetching live quotes: {str(e)}")
        return {"quotes": {}}
@router.get("/search")
def search_symbols(query: str, exclude_indices: bool = False, db: Session = Depends(get_db)):
    """
    Search for companies and indices by symbol or name
    exclude_indices: Set to True to only return equities (for analyst mode)
    """
    if not query or len(query) < 2:
        return []
    
    from ..constants.indices import STOCK_INDICES
    
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
    from ..database import Company
    
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
    
    return results_list[:15]  # Limit to 15 total results

@router.get("/sectors")
def get_sectors(db: Session = Depends(get_db)):
    """
    Get list of all available sectors
    """
    from ..database import Company
    
    # query distinct sectors
    sectors = db.query(Company.sector).distinct().filter(Company.sector != None).order_by(Company.sector).all()
    
    return {"sectors": [s[0] for s in sectors]}

@router.get("/watchlist")
def get_watchlist(db: Session = Depends(get_db)):
    """Get user watchlist"""
    from ..database import Watchlist, HistoricalPrice, Company
    
    # Get symbols
    items = db.query(Watchlist).order_by(Watchlist.added_at.desc()).all()
    if not items:
        return []
        
    results = []
    # For each symbol, fetch latest price/data
    for item in items:
        # Try finding basic info
        ltp = 0
        change = 0
        change_pct = 0
        
        # Determine strict symbol for query
        sym = item.symbol
        
        # Try finding live price from historical (or fyers if we had it connected here)
        # For now, get latest historical
        company = db.query(Company).filter(Company.symbol == sym).first()
        if company:
            latest_price = db.query(HistoricalPrice).filter(HistoricalPrice.company_id == company.id).order_by(HistoricalPrice.date.desc()).first()
            if latest_price:
                ltp = latest_price.close
                # Calculate change (vs prev close approx)
                prev = latest_price.open # Approximate
                change = ltp - prev
                change_pct = (change / prev * 100) if prev else 0
        
        results.append({
            "symbol": item.symbol,
            "ltp": ltp,
            "change": change,
            "change_pct": change_pct,
            "instrument_type": item.instrument_type
        })
        
    return results

@router.post("/watchlist")
def add_to_watchlist(item: dict, db: Session = Depends(get_db)):
    """Add to watchlist"""
    from ..database import Watchlist
    
    symbol = item.get('symbol')
    inst_type = item.get('instrument_type', 'EQ')
    
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol required")
        
    existing = db.query(Watchlist).filter(Watchlist.symbol == symbol).first()
    if existing:
        return {"message": "Already in watchlist"}
        
    new_item = Watchlist(symbol=symbol, instrument_type=inst_type)
    db.add(new_item)
    db.commit()
    return {"message": "Added"}

@router.delete("/watchlist/{symbol}")
def remove_from_watchlist(symbol: str, db: Session = Depends(get_db)):
    """Remove from watchlist"""
    from ..database import Watchlist
    
    db.query(Watchlist).filter(Watchlist.symbol == symbol).delete()
    db.commit()
    return {"message": "Removed"}
