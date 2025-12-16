"""
Trending Stocks Scanner - Query pre-calculated indicators from database
All indicators are already stored in HistoricalPrice table
"""
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, desc
from typing import List, Dict
from datetime import datetime, timedelta
import pytz

from .database import Company, HistoricalPrice


def calculate_trending_stocks(
    db: Session,
    filter_type: str = 'ALL',
    limit: int = 50,
    sort_by: str = None,
    sort_order: str = 'desc'
) -> List[Dict]:
    """
    Get trending stocks using LIVE market data + Historical Baselines
    """
    from .fyers_direct import get_fyers_quotes
    
    # 1. Get Historical Baselines (Avg Volume, 52W High, etc.)
    # -------------------------------------------------------
    latest_date_subquery = db.query(
        HistoricalPrice.company_id,
        func.max(HistoricalPrice.date).label('latest_date')
    ).group_by(HistoricalPrice.company_id).subquery()
    
    # Get 52W Highs for all stocks
    high_52w_subquery = db.query(
        HistoricalPrice.company_id,
        func.max(HistoricalPrice.high).label('high_52w')
    ).filter(
        HistoricalPrice.date >= (datetime.now() - timedelta(days=365))
    ).group_by(HistoricalPrice.company_id).subquery()
    
    # Query all active F&O stocks with their latest historical stats
    candidates = db.query(
        Company.symbol,
        Company.name,
        HistoricalPrice.avg_volume,
        HistoricalPrice.rsi,
        HistoricalPrice.ema_20,
        HistoricalPrice.ema_50,
        HistoricalPrice.volume_percentile,
        HistoricalPrice.close.label('hist_close'),
        high_52w_subquery.c.high_52w
    ).join(
        HistoricalPrice, Company.id == HistoricalPrice.company_id
    ).join(
        latest_date_subquery,
        and_(
            HistoricalPrice.company_id == latest_date_subquery.c.company_id,
            HistoricalPrice.date == latest_date_subquery.c.latest_date
        )
    ).outerjoin(
        high_52w_subquery,
        Company.id == high_52w_subquery.c.company_id
    ).filter(
        Company.is_fno == True,
        Company.is_active == True
    ).all()
    
    # 2. Fetch Live Quotes
    # --------------------
    symbols = [c.symbol for c in candidates]
    live_quotes = {}
    
    # Batch fetching (50 at a time) to be safe
    batch_size = 50
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        try:
            quotes = get_fyers_quotes(batch)
            if quotes:
                live_quotes.update(quotes)
        except Exception as e:
            print(f"Error fetching batch {i}: {e}")
        
    # 3. Merge & Filter
    # -----------------
    results = []
    
    for c in candidates:
        quote = live_quotes.get(c.symbol)
        
        # Use live data if available, else fallback to historical close
        if quote:
            price = float(quote.get('ltp', 0))
            volume = int(quote.get('volume', 0))
            change_p = float(quote.get('change_pct', 0))
        else:
            price = float(c.hist_close or 0)
            volume = 0 # Assume 0 volume if no live data
            change_p = 0.0
            
        # Prepare object
        avg_vol = int(c.avg_volume) if c.avg_volume else 0
        high_52w = float(c.high_52w) if c.high_52w else 0
        
        
        item = {
            "symbol": c.symbol,
            "name": c.name,
            "close": price,
            "change_pct": change_p,
            "volume": volume,
            "avg_volume": avg_vol,
            "ema20": float(c.ema_20) if c.ema_20 else 0,
            "ema50": float(c.ema_50) if c.ema_50 else 0,
            "atr_pct": round((price * 0.02), 2) if price > 0 else 0,  # Placeholder: 2% of price
            "rsi": float(c.rsi) if c.rsi else 50,
            "vol_percentile": float(c.volume_percentile) if c.volume_percentile else 50,
            "high_52w": high_52w
        }
        
        # --- FILTERS ---
        include = False
        
        if filter_type == 'ALL':
            include = True
            
        elif filter_type == 'VOLUME_SHOCKER':
            # Live Volume > 3x Avg Volume
            if avg_vol > 0 and volume > (avg_vol * 3):
                include = True
                
        elif filter_type == 'PRICE_SHOCKER':
            # Change > 2% or < -2% (Standard definition of shock/movement)
            # User complained about < 5%, we can set it to 3% or 4% to be stricter
            if abs(change_p) >= 3:
                include = True
                
        elif filter_type == '52W_HIGH':
            # Within 1% of 52W High
            if high_52w > 0 and price >= (high_52w * 0.99):
                include = True
                
        elif filter_type == '52W_LOW':
            # Not implemented fully (need 52w low query from history), pass for now or skip
            pass
            
        if include:
            results.append(item)
            
    # 4. Sort
    # -------
    if sort_by in ['change_pct', 'close', 'volume', 'rsi']:
        reverse = (sort_order == 'desc')
        results.sort(key=lambda x: x.get(sort_by, 0), reverse=reverse)
        
    elif filter_type == 'VOLUME_SHOCKER':
        # Default sort: Volume magnitude relative to average
        results.sort(key=lambda x: (x['volume'] / x['avg_volume'] if x['avg_volume'] > 0 else 0), reverse=True)
        
    elif filter_type == '52W_HIGH':
        # Default sort: Proximity/Breakout above 52W High
        results.sort(key=lambda x: (x['close'] / x['high_52w'] if x['high_52w'] > 0 else 0), reverse=True)
        
    elif filter_type == 'PRICE_SHOCKER':
        # Sort by absolute change
        results.sort(key=lambda x: abs(x['change_pct']), reverse=True)
        
    return results[:limit]
