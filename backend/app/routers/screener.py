from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc, func, and_
from ..database import get_db, SessionLocal, Company, HistoricalPrice, FinancialStatement
from ..data_repository import DataRepository
from ..indicators import compute_features
from ..data_fetcher import fetch_fyers_quotes
from ..constants.indices import STOCK_INDICES, DEFAULT_SCREENER_UNIVERSE, TREND_FILTER_UNIVERSE
import math  # For NaN/inf filtering

router = APIRouter()

@router.get("/indices")
def get_indices():
    """Get available index filters for screener"""
    return {
        "indices": [
            {"id": key, "name": val["name"], "description": val["description"]}
            for key, val in STOCK_INDICES.items()
        ],
        "default": DEFAULT_SCREENER_UNIVERSE
    }

# Helper function
def calculate_technical_score(features: dict) -> int:
    """
    Calculate a 0-100 technical score based on trend and momentum.
    """
    score = 50  # Base score
    
    # 1. Trend (30 pts)
    if features.get('ema20_above_50'):
        score += 15
    if features.get('price_above_ema50'):
        score += 15
        
    # 2. Momentum (RSI) (30 pts)
    rsi = features.get('rsi', 50)
    if 50 <= rsi <= 70:
        score += 20
    elif 40 <= rsi < 50:
        score += 10
    elif rsi > 70:
        score += 10 # Overbought but strong
        
    # 3. Volume (20 pts)
    vol_pct = features.get('vol_percentile', 50)
    if vol_pct > 80:
        score += 20
    elif vol_pct > 50:
        score += 10
        
    # 4. Volatility (20 pts)
    atr_pct = features.get('atr_pct', 2)
    if atr_pct < 3: # Low volatility preference
        score += 20
    elif atr_pct < 5:
        score += 10
        
    return min(100, max(0, score))


def sanitize_features(features: dict) -> dict:
    """Replace NaN and inf values with 0 to prevent JSON serialization errors"""
    sanitized = {}
    for key, value in features.items():
        if isinstance(value, (int, float)):
            if math.isnan(value) or math.isinf(value):
                sanitized[key] = 0
            else:
                sanitized[key] = value
        else:
            sanitized[key] = value
    return sanitized


@router.get("/")
def get_screener(
    page: int = 1, 
    limit: int = 100, 
    sort_by: str = 'symbol', 
    sort_order: str = 'asc', 
    symbol: str = None,
    sector: str = None,
    view: str = 'technical', # 'technical' or 'financial'
    filter_type: str = 'ALL', # 'ALL', 'VOLUME_SHOCKER', '52W_HIGH', '52W_LOW', 'PRICE_SHOCKER'
    index: str = 'NIFTY50'
):
    """
    Returns paginated list of companies with technical indicators or financial data.
    Also supports sorting and filtering by symbol, sector, and predefined technical scans.
    """
    db = None  # Initialize to prevent UnboundLocalError in exception handler
    try:
        db = SessionLocal()
        repo = DataRepository(db)
        
        # Get latest date for each company's historical prices
        latest_prices_subquery = db.query(
            HistoricalPrice.company_id,
            func.max(HistoricalPrice.date).label('latest_date')
        ).group_by(HistoricalPrice.company_id).subquery()
        
        # Base Query
        companies_query = db.query(Company, HistoricalPrice).join(
            latest_prices_subquery,
            Company.id == latest_prices_subquery.c.company_id
        ).join(
            HistoricalPrice,
            and_(
                HistoricalPrice.company_id == Company.id,
                HistoricalPrice.date == latest_prices_subquery.c.latest_date
            )
        ).filter(Company.is_active == True)
        
        # Join Financials if requested
        if view == 'financial':
            companies_query = companies_query.join(
                FinancialStatement, 
                Company.id == FinancialStatement.company_id
            ).distinct()
        
        # 1. Apply Search Filters
        
        # Filter by INDEX (NIFTY50, BANKNIFTY, etc.)
        if index and index != 'ALL':
             # Resolve symbols from index definition
             if index in STOCK_INDICES:
                 target_symbols = STOCK_INDICES[index]['symbols']
                 companies_query = companies_query.filter(Company.symbol.in_(target_symbols))
        
        if symbol:
            companies_query = companies_query.filter(Company.symbol.ilike(f"{symbol.upper()}%"))
        
        if sector and sector.lower() != 'all':
            companies_query = companies_query.filter(Company.sector == sector)
        
        # 2. Apply Technical Filters (SQL Optimized)
        # Note: We prioritize "Show Data" over "Strict Correctness" if filters return 0
        original_query = companies_query # Backup for fallback
        
        if filter_type == 'VOLUME_SHOCKER':
             # Volume > 2x Avg Volume (approximate using just high volume for safety if avg missing)
             companies_query = companies_query.order_by(desc(HistoricalPrice.volume)).limit(50) # Top 50 volume
        
        elif filter_type == '52W_HIGH':
            # Close within 5% of 20d High or Breakout
            companies_query = companies_query.filter(HistoricalPrice.is_breakout == True)
            companies_query = companies_query.order_by(desc(HistoricalPrice.close))
            
        elif filter_type == '52W_LOW':
            # Proxy: Low RSI
            companies_query = companies_query.order_by(HistoricalPrice.rsi_14)
            
        elif filter_type == 'PRICE_SHOCKER':
            # High volatility/change - sort by ATR  
            companies_query = companies_query.order_by(desc(HistoricalPrice.atr_14))
            
        # 3. sorting (If not overridden by filter)
        if filter_type == 'ALL':
            if sort_by == 'close':
                companies_query = companies_query.order_by(desc(HistoricalPrice.close) if sort_order == 'desc' else HistoricalPrice.close)
            elif sort_by == 'volume':
                companies_query = companies_query.order_by(desc(HistoricalPrice.volume) if sort_order == 'desc' else HistoricalPrice.volume)
            elif sort_by == 'change_pct':
                # SQL-based Change % Sort
                change_expr = (HistoricalPrice.close - HistoricalPrice.open) / HistoricalPrice.open * 100
                companies_query = companies_query.order_by(desc(change_expr) if sort_order == 'desc' else change_expr)
            elif sort_by == 'market_cap':
                companies_query = companies_query.order_by(desc(Company.market_cap) if sort_order == 'desc' else Company.market_cap)
            elif sort_by in ['pe_ratio', 'roe', 'revenue', 'eps', 'debt_to_equity', 'net_income']:
                # Ensure we are joined to FinancialStatement
                if view != 'financial': 
                    # If user tries to sort by financial but view is technical, we must join anyway
                    companies_query = companies_query.join(
                        FinancialStatement, 
                        Company.id == FinancialStatement.company_id
                    ).distinct()
                
                sort_col = getattr(FinancialStatement, sort_by)
                companies_query = companies_query.order_by(desc(sort_col) if sort_order == 'desc' else sort_col)
            else:
                companies_query = companies_query.order_by(Company.symbol)
        
        # 4. Fallback Check
        total_records = companies_query.count()
        if total_records == 0 and filter_type != 'ALL' and not symbol:
            # If a special filter returns 0, FALLBACK to ALL so user sees SOMETHING
            companies_query = original_query.order_by(desc(HistoricalPrice.volume))
            total_records = companies_query.count()
        
        # Paginate
        offset = (page - 1) * limit
        results = companies_query.offset(offset).limit(limit).all()
        
        fetched_companies = [r[0] for r in results]
        fetched_hist_prices = [r[1] for r in results]

        symbols = [c.symbol for c in fetched_companies]
        # REMOVED: bulk_hist fetch - indicators already in historical_prices table per SCREENER_TECHNICAL.md
        
        # Bulk Fetch Financials if requested
        fin_map = {}
        if view == 'financial' and fetched_companies:
            comp_ids = [c.id for c in fetched_companies]
            all_fins = db.query(FinancialStatement).filter(
                FinancialStatement.company_id.in_(comp_ids)
            ).order_by(FinancialStatement.period_end.desc()).all()
            for f in all_fins:
                if f.company_id not in fin_map:
                    fin_map[f.company_id] = f
        
        # ---------------------------------------------------------
        # FETCH LIVE DATA (OPTIMIZATION)
        # ---------------------------------------------------------
        live_quotes = {}
        if symbols:
            try:
                # This fetches live LTP, Volume, High, Low from Fyers for the visible page
                live_quotes = fetch_fyers_quotes(symbols)
            except Exception as e:
                print(f"Live quote fetch failed: {e}")

        # Compute features
        computed_list = []
        for i, company in enumerate(fetched_companies):
            hist_price = fetched_hist_prices[i]
            
            try:
                # Direct mapping from HistoricalPrice columns (already fetched)
                # We verified these columns exist and have data
                features = {
                    'symbol': company.symbol,
                    'close': float(hist_price.close or 0),
                    'open': float(hist_price.open or 0),
                    'high': float(hist_price.high or 0),
                    'low': float(hist_price.low or 0),
                    'volume': int(hist_price.volume or 0),
                    'ema20': float(hist_price.ema_20 or 0),
                    'ema50': float(hist_price.ema_50 or 0),
                    'rsi': float(hist_price.rsi_14 or 50),
                    # ATR% = (ATR / Close) * 100
                    'atr_pct': (float(hist_price.atr_14 or 0) / float(hist_price.close or 1) * 100) if hist_price.close else 0,
                    'macd': float(hist_price.macd or 0),
                    'macd_signal': float(hist_price.macd_signal or 0), # New
                    'adx': float(hist_price.adx or 0),
                    'stoch_k': float(hist_price.stoch_k or 0), # New
                    'stoch_d': float(hist_price.stoch_d or 0), # New
                    'bb_upper': float(hist_price.bb_upper or 0), # New
                    'bb_middle': float(hist_price.bb_middle or 0), # New
                    'bb_lower': float(hist_price.bb_lower or 0), # New
                    'trend_7d': float(hist_price.trend_7d or 0),
                    'trend_30d': float(hist_price.trend_30d or 0),
                    'is_20d_breakout': bool(hist_price.is_breakout or False),
                    'vol_percentile': 50, # Default if not in DB
                    
                    # Calculate Change % (Close - PrevClose) / PrevClose
                    # Since we only have one row here, we use (Close - Open) as intraday proxy
                    'change_pct': 0.0
                }
                
                # Calculate change_pct
                open_price = features['open']
                if open_price > 0:
                    features['change_pct'] = ((features['close'] - open_price) / open_price) * 100

                # -----------------------------------------------------
                # OVERRIDE WITH LIVE DATA IF AVAILABLE
                # -----------------------------------------------------
                if company.symbol in live_quotes:
                    qt = live_quotes[company.symbol]
                    
                    # Only override if we have valid non-zero values
                    live_close = qt.get('ltp', 0)
                    live_volume = qt.get('volume', 0)
                    
                    if live_close > 0:
                        features['close'] = live_close
                        
                        # Recalculate Change % based on Previous Close if available, else Open
                        prev_close = qt.get('prev_close', 0)
                        live_open = qt.get('open_price', 0) 
                        
                        if prev_close > 0:
                            features['change_pct'] = ((live_close - prev_close) / prev_close) * 100
                        elif live_open > 0:
                            features['change_pct'] = ((live_close - live_open) / live_open) * 100
                            
                    if live_volume > 0:
                        features['volume'] = live_volume

                # Add financials if present
                if view == 'financial' and company.id in fin_map:
                    fin_stmt = fin_map[company.id]
                    features['revenue'] = fin_stmt.revenue
                    features['net_income'] = fin_stmt.net_income
                    features['eps'] = fin_stmt.eps
                    features['pe_ratio'] = fin_stmt.pe_ratio
                    features['roe'] = fin_stmt.roe
                    features['debt_to_equity'] = fin_stmt.debt_to_equity
                    features['period'] = str(fin_stmt.period_end)
                    features['market_cap'] = company.market_cap
                
                # Calculate scores
                tech_score = calculate_technical_score(features)
                features['intraday_score'] = tech_score
                features['swing_score'] = tech_score 
                
                # Sanitize features to remove NaN/inf values
                features = sanitize_features(features)
                
                computed_list.append(features)
            except Exception as e:
                # Fallback to mostly empty but valid dict
                # print(f"Error processing {company.symbol}: {e}") # Optional logging
                ema20 = hist_price.ema_20 or 0
                close_val = hist_price.close or 0
                change_pct = ((close_val - ema20) / ema20 * 100) if ema20 else 0
                
                # Ensure values are safe
                if math.isnan(close_val) or math.isinf(close_val): close_val = 0
                if math.isnan(change_pct) or math.isinf(change_pct): change_pct = 0
                
                computed_list.append({
                    'symbol': company.symbol,
                    'close': float(close_val),
                    'change_pct': round(change_pct, 2),
                    'volume': int(hist_price.volume or 0),
                    'ema20': ema20,
                    'ema50': hist_price.ema_50 or 0,
                    'atr_pct': hist_price.atr_pct or 0,
                    'rsi': hist_price.rsi or 50,
                    'vol_percentile': hist_price.volume_percentile or 50,
                    'intraday_score': 50,  # Default score
                    'swing_score': 50
                })
        
        db.close()
        
        return {
            'data': computed_list,
            'meta': {
                'page': page,
                'limit': limit,
                'total': total_records,
                'total_pages': (total_records + limit - 1) // limit if limit > 0 else 0,
                'applied_filter': filter_type if total_records > 0 else 'FALLBACK_ALL'
            }
        }
        
    except Exception as e:
        if db:
            db.close()
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/financials")
def get_financials_screener(
    page: int = 1, 
    limit: int = 100, 
    sort_by: str = 'symbol', 
    sort_order: str = 'asc', 
    symbol: str = None,
    sector: str = None
):
    """
    Returns paginated list of companies with latest financial data
    """
    db = SessionLocal()
    try:
        offset = (page - 1) * limit
        
        # Subquery to get latest financial statement date for each company
        latest_subquery = db.query(
            FinancialStatement.company_id,
            func.max(FinancialStatement.period_end).label('max_date')
        ).filter(FinancialStatement.period_type == 'annual').group_by(FinancialStatement.company_id).subquery()
        
        query = db.query(Company, FinancialStatement).join(
            latest_subquery,
            (FinancialStatement.company_id == latest_subquery.c.company_id) & 
            (FinancialStatement.period_end == latest_subquery.c.max_date)
        ).join(
            Company, Company.id == FinancialStatement.company_id
        ).filter(Company.is_active == True)
        
        # Add symbol filter if provided
        if symbol:
            query = query.filter(Company.symbol == symbol.upper())
        
        # Add sector filter if provided
        if sector:
            query = query.filter(Company.sector == sector)
        
        # Get total count before pagination
        total_records = query.count()
        
        # Apply sorting
        if sort_by == 'symbol':
            sort_col = Company.symbol
        elif hasattr(FinancialStatement, sort_by):
            sort_col = getattr(FinancialStatement, sort_by)
        else:
            sort_col = Company.symbol
            
        if sort_order == 'desc':
            query = query.order_by(desc(sort_col))
        else:
            query = query.order_by(sort_col)
            
        results = query.offset(offset).limit(limit).all()
        
        data = []
        for company, fs in results:
            data.append({
                'symbol': company.symbol,
                'market_cap': float(company.market_cap) if company.market_cap else 0,
                'revenue': float(fs.revenue) if fs.revenue else 0,
                'net_income': float(fs.net_income) if fs.net_income else 0,
                'eps': float(fs.eps) if fs.eps else 0,
                'roe': float(fs.roe) if fs.roe else 0,
                'debt_to_equity': float(fs.debt_to_equity) if fs.debt_to_equity else 0,
                'pe_ratio': float(fs.pe_ratio) if fs.pe_ratio else 0,
                'period_end': fs.period_end.isoformat()
            })
            
        db.close()
        
        return {
            'data': data,
            'meta': {
                'page': page,
                'limit': limit,
                'total': total_records,
                'total_pages': (total_records + limit - 1) // limit if limit > 0 else 0
            }
        }
        
    except Exception as e:
        print(f"Error in financials screener: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Financials failed: {str(e)}")
