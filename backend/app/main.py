from fastapi import FastAPI, HTTPException, WebSocket, UploadFile, File, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, desc, func, or_, and_
from sqlalchemy.orm import sessionmaker, Session
from typing import List, Optional, Dict, Any
import json
import numpy as np
from datetime import date, datetime, timezone
from dotenv import load_dotenv
from .database import Base, Company, FinancialStatement, HistoricalPrice, engine, SessionLocal, UserPortfolio, PortfolioPosition, ComputedRiskMetric, get_db
from .data_repository import DataRepository
from .portfolio_risk import PortfolioRiskEngine
import pandas as pd

# Import Smart Trader API routers
from .smart_trader_api import router as smart_trader_router

# Load environment variables from .env file
load_dotenv()

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include Smart Trader routers
app.include_router(smart_trader_router)

# Include AI Insight router
from .ai_insight_api import router as ai_insight_router
app.include_router(ai_insight_router)

# Start Cache Manager
# Initialize Database
Base.metadata.create_all(bind=engine)

# --- Compute Features (EMA, RSI, etc) ---
from .indicators import compute_features

# --- Cache for Screeners ---
SCREENER_CACHE = {
    'last_update': None,
    'data': []
}

@app.get("/")
def read_root():
    return {"status": "ok", "message": "AlgoTrading Backend Running"}


@app.get("/api/sectors")
async def get_sectors():
    """Get list of unique sectors for filter dropdown"""
    db = SessionLocal()
    try:
        sectors = db.query(Company.sector).filter(
            Company.sector.isnot(None),
            Company.sector != ''
        ).distinct().order_by(Company.sector).all()
        
        return {
            "sectors": [s[0] for s in sectors]
        }
    finally:
        db.close()

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
    elif rsi > 70:
        score += 10 # Overbought but strong
    elif rsi < 30:
        score -= 10 # Oversold weak
        
    # 3. Volatility/Action (20 pts)
    if features.get('is_20d_breakout'):
        score += 20
        
    # 4. Volume (20 pts)
    if features.get('vol_percentile', 0) > 80:
        score += 20
    elif features.get('vol_percentile', 0) > 50:
        score += 10
        
    return min(100, max(0, score))

@app.get("/api/screener")
async def get_screener(
    page: int = 1, 
    limit: int = 100, 
    sort_by: str = 'symbol', 
    sort_order: str = 'asc', 
    symbol: str = None,
    sector: str = None
):
    """
    Returns paginated list of companies with technical indicators.
    Also supports sorting and filtering by symbol and sector.
    """
    db = SessionLocal()
    repo = DataRepository(db)
    try:
        from .database import HistoricalPrice
        from sqlalchemy import and_
        
        # Get latest date for each company's historical prices
        latest_prices_subquery = db.query(
            HistoricalPrice.company_id,
            func.max(HistoricalPrice.date).label('latest_date')
        ).group_by(HistoricalPrice.company_id).subquery()
        
        # Join companies with their latest historical prices
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
        
        # Add symbol filter if provided (partial match)
        if symbol:
            companies_query = companies_query.filter(Company.symbol.ilike(f"{symbol.upper()}%"))
        
        # Add sector filter if provided
        if sector:
            companies_query = companies_query.filter(Company.sector == sector)
        
        # Apply sorting at database level for close price
        if sort_by == 'close':
            if sort_order == 'desc':
                companies_query = companies_query.order_by(desc(HistoricalPrice.close))
            else:
                companies_query = companies_query.order_by(HistoricalPrice.close)
        elif sort_by == 'volume':
            if sort_order == 'desc':
                companies_query = companies_query.order_by(desc(HistoricalPrice.volume))
            else:
                companies_query = companies_query.order_by(HistoricalPrice.volume)
        elif sort_by == 'change_pct':
            # Calculate change_pct expression: (close - ema_20) / ema_20
            # Handle division by zero/null by ordering them last
            change_expr = (HistoricalPrice.close - HistoricalPrice.ema_20) / HistoricalPrice.ema_20
            if sort_order == 'desc':
                companies_query = companies_query.order_by(desc(change_expr))
            else:
                companies_query = companies_query.order_by(change_expr)
        else:
            # Default to symbol sorting
            companies_query = companies_query.order_by(Company.symbol)
        
        total_records = companies_query.count()
        
        # Paginate
        offset = (page - 1) * limit
        results = companies_query.offset(offset).limit(limit).all()
        
        # Get all symbols for bulk fetch
        symbols = [c.symbol for c, _ in results]
        bulk_hist = repo.get_bulk_historical_prices(symbols, days=200)
        
        # Compute features for the paginated results only
        computed_list = []
        for company, hist_price in results:
            try:
                hist = bulk_hist.get(company.symbol)
                if hist is not None and not hist.empty:
                    features = compute_features(company.symbol, hist)
                else:
                    # Use the latest price from the join
                    features = {
                        'symbol': company.symbol,
                        'close': float(hist_price.close),
                        'volume': int(hist_price.volume),
                        'ema20': hist_price.ema_20 or 0,
                        'ema50': hist_price.ema_50 or 0,
                        'atr_pct': hist_price.atr_pct or 0,
                        'rsi': hist_price.rsi or 50,
                        'vol_percentile': hist_price.volume_percentile or 50
                    }
                
                # Ensure change_pct is present
                if 'change_pct' not in features:
                    ema20 = features.get('ema20', 0)
                    close = features.get('close', 0)
                    features['change_pct'] = ((close - ema20) / ema20 * 100) if ema20 else 0
                
                if 'symbol' not in features:
                    features['symbol'] = company.symbol
                
                # Calculate scores
                tech_score = calculate_technical_score(features)
                features['intraday_score'] = tech_score
                features['swing_score'] = tech_score # Using same model for now
                
                computed_list.append(features)
            except Exception as e:
                # Fallback to basic data
                ema20 = hist_price.ema_20 or 0
                change_pct = ((hist_price.close - ema20) / ema20 * 100) if ema20 else 0
                
                computed_list.append({
                    'symbol': company.symbol,
                    'close': float(hist_price.close),
                    'change_pct': round(change_pct, 2),
                    'volume': int(hist_price.volume),
                    'ema20': ema20,
                    'ema50': hist_price.ema_50 or 0,
                    'atr_pct': hist_price.atr_pct or 0,
                    'rsi': hist_price.rsi or 50,
                    'vol_percentile': hist_price.volume_percentile or 50
                })
                continue
        
        db.close()
        
        return {
            'data': computed_list,
            'meta': {
                'page': page,
                'limit': limit,
                'total': total_records,
                'total_pages': (total_records + limit - 1) // limit if limit > 0 else 0
            }
        }
        
    except Exception as e:
        db.close()
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/screener/financials")
async def get_financials_screener(
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
        # import traceback
        # traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Financials failed: {str(e)}")

@app.get("/api/quotes/live")
async def get_live_quotes(symbols: str, db: Session = Depends(get_db)):
    """
    Get live quotes for multiple symbols
    symbols: comma-separated list (e.g., "RELIANCE,TCS,INFY")
    """
    try:
        symbol_list = [s.strip() for s in symbols.split(',')]
        
        # Fetch live quotes from Fyers
        from .fyers_direct import get_fyers_quotes
        quotes = get_fyers_quotes(symbol_list)
        
        return {"quotes": quotes}
        
    except Exception as e:
        print(f"Error fetching live quotes: {str(e)}")
        # Return empty quotes instead of error to prevent frontend issues
        return {"quotes": {}}


@app.post("/api/upload/bulk-financials")
async def upload_financials(files: List[UploadFile] = File(...)):
    db = SessionLocal()
    from .excel_parser import SmartFinancialParser
    parser = SmartFinancialParser()
    
    try:
        all_metadata = []
        stats = {
            'total_processed': 0,
            'symbols_updated': [],
            'symbols_not_found': []
        }
        all_errors = []
        total_success = 0
        symbols_updated = []
        symbols_not_found = []

        for file in files:
            try:
                # Read file
                contents = await file.read()
                
                # Use AI parser
                parsed_df, metadata = parser.parse_excel(contents, file.filename)
                all_metadata.append({
                    'filename': file.filename,
                    **metadata
                })
                
                print(f"Parsed {file.filename}: {len(parsed_df)} rows")
                if not parsed_df.empty:
                    print(f"First row: {parsed_df.iloc[0].to_dict()}")
                
                file_success_count = 0
                
                for _, row in parsed_df.iterrows():
                    try:
                        symbol = row['symbol']
                        print(f"Processing symbol: {symbol}, revenue: {row.get('revenue', 0)}")
                        
                        # Find Company - Strict Match Only
                        company = db.query(Company).filter(Company.symbol == symbol).first()
                        if not company:
                            # Try finding with symbol matching logic first
                            matched_symbol = parser._match_symbol_from_db(symbol)
                            if matched_symbol != symbol:
                                company = db.query(Company).filter(Company.symbol == matched_symbol).first()
                        
                        if not company:
                            print(f"Skipping {symbol}: Not found in master list")
                            symbols_not_found.append(symbol)
                            continue
                                
                        # Upsert Financial Statement
                        today = date.today()
                        existing_fs = db.query(FinancialStatement).filter(
                            FinancialStatement.company_id == company.id,
                            FinancialStatement.period_type == 'annual',
                            FinancialStatement.period_end == today
                        ).first()

                        if existing_fs:
                            existing_fs.revenue = row['revenue']
                            existing_fs.net_income = row['net_income']
                            existing_fs.eps = row['eps']
                            existing_fs.roe = row['roe']
                            existing_fs.debt_to_equity = row['debt_to_equity']
                            existing_fs.pe_ratio = row['pe_ratio']
                            existing_fs.source = 'ai_bulk_upload'
                            print(f"Updated existing financial statement for {company.symbol}")
                        else:
                            fs = FinancialStatement(
                                company_id=company.id,
                                period_end=today,
                                period_type='annual',
                                revenue=row['revenue'],
                                net_income=row['net_income'],
                                eps=row['eps'],
                                roe=row['roe'],
                                debt_to_equity=row['debt_to_equity'],
                                pe_ratio=row['pe_ratio'],
                                source='ai_bulk_upload'
                            )
                            db.add(fs)
                            print(f"Created new financial statement for {company.symbol}")
                        
                        symbols_updated.append(company.symbol)
                        file_success_count += 1
                        
                    except Exception as row_e:
                        print(f"❌ Row error for {symbol}: {str(row_e)}")
                        all_errors.append(f"Row error for {symbol}: {str(row_e)}")
                        continue
                
                db.commit()
                total_success += file_success_count
                
            except Exception as file_e:
                all_errors.append(f"File {file.filename}: {str(file_e)}")
                continue
        
        # Build detailed message
        message_parts = [f"Successfully processed {total_success} records from {len(files)} files"]
        if symbols_updated:
            message_parts.append(f"Updated: {', '.join(sorted(list(set(symbols_updated)))[:10])}")
        if symbols_not_found:
            message_parts.append(f"Symbols not in database: {', '.join(sorted(list(set(symbols_not_found)))[:10])}")
        
        full_message = ". ".join(message_parts)
            
        return {
            "message": full_message,
            "metadata": all_metadata,
            "stats": {
                'total_processed': total_success,
                'symbols_updated': list(set(symbols_updated)),
                'symbols_not_found': list(set(symbols_not_found))
            },
            "errors": all_errors
        }
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# ==================== PORTFOLIO RISK MANAGEMENT ====================

from .database import UserPortfolio, PortfolioPosition, ComputedRiskMetric
from .portfolio_risk import PortfolioRiskEngine
from pydantic import BaseModel

class PositionInput(BaseModel):
    symbol: str
    invested_value: float
    quantity: Optional[float] = None
    avg_buy_price: Optional[float] = None

class PortfolioCreate(BaseModel):
    portfolio_name: str
    description: Optional[str] = None
    positions: List[PositionInput]

@app.post("/api/portfolios")
async def create_portfolio(portfolio: PortfolioCreate):
    """Create a new portfolio with positions"""
    db = SessionLocal()
    try:
        # Create portfolio
        new_portfolio = UserPortfolio(
            portfolio_name=portfolio.portfolio_name,
            description=portfolio.description
        )
        db.add(new_portfolio)
        db.flush()
        
        # Calculate total invested value
        total_invested = sum(p.invested_value for p in portfolio.positions)
        
        # Add positions
        for pos in portfolio.positions:
            # Find company
            company = db.query(Company).filter(Company.symbol == pos.symbol.upper()).first()
            if not company:
                raise HTTPException(status_code=404, detail=f"Symbol {pos.symbol} not found")
            
            # Calculate allocation
            allocation_pct = (pos.invested_value / total_invested) * 100
            
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
        
        return {
            "id": new_portfolio.id,
            "portfolio_name": new_portfolio.portfolio_name,
            "message": "Portfolio created successfully"
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/portfolios")
async def list_portfolios():
    """List all portfolios"""
    db = SessionLocal()
    try:
        portfolios = db.query(UserPortfolio).all()
        return {
            "portfolios": [
                {
                    "id": p.id,
                    "portfolio_name": p.portfolio_name,
                    "description": p.description,
                    "created_at": p.created_at.isoformat(),
                    "num_positions": len(p.positions)
                }
                for p in portfolios
            ]
        }
    finally:
        db.close()

@app.get("/api/portfolios/{portfolio_id}")
async def get_portfolio(portfolio_id: int):
    """Get portfolio details with positions"""
    db = SessionLocal()
    try:
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
    finally:
        db.close()

@app.delete("/api/portfolios/{portfolio_id}")
async def delete_portfolio(portfolio_id: int):
    """Delete a portfolio"""
    db = SessionLocal()
    try:
        portfolio = db.query(UserPortfolio).filter(UserPortfolio.id == portfolio_id).first()
        if not portfolio:
            raise HTTPException(status_code=404, detail="Portfolio not found")
        
        db.delete(portfolio)
        db.commit()
        return {"message": "Portfolio deleted successfully"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/portfolios/{portfolio_id}/analyze")
async def analyze_portfolio(portfolio_id: int, lookback_days: int = 252):
    """Comprehensive portfolio risk analysis"""
    db = SessionLocal()
    repo = DataRepository(db)
    
    try:
        print(f"[DEBUG] Starting analysis for portfolio {portfolio_id}")
        
        # Get portfolio
        portfolio = db.query(UserPortfolio).filter(UserPortfolio.id == portfolio_id).first()
        if not portfolio:
            raise HTTPException(status_code=404, detail="Portfolio not found")
        
        print(f"[DEBUG] Portfolio found: {portfolio.portfolio_name}")
        print(f"[DEBUG] Number of positions: {len(portfolio.positions)}")
        
        if len(portfolio.positions) == 0:
            raise HTTPException(status_code=400, detail="Portfolio has no positions")
        
        # Get symbols and weights
        symbols = [pos.company.symbol for pos in portfolio.positions]
        weights = np.array([pos.allocation_pct / 100 for pos in portfolio.positions])
        
        print(f"[DEBUG] Symbols: {symbols}")
        print(f"[DEBUG] Weights: {weights}")
        
        # Fetch historical prices
        prices_dict = {}
        missing_data_symbols = []
        for symbol in symbols:
            print(f"[DEBUG] Fetching historical data for {symbol}")
            hist = repo.get_historical_prices(symbol, days=lookback_days)
            if hist is None or hist.empty:
                missing_data_symbols.append(symbol)
                print(f"[DEBUG] No historical data found for {symbol}")
            else:
                prices_dict[symbol] = hist['Close']
                print(f"[DEBUG] Got {len(hist)} days of data for {symbol}")
        
        # Check if any symbols are missing data
        if missing_data_symbols:
            error_msg = f"No historical data found for: {', '.join(missing_data_symbols)}. Please ensure these symbols exist in the database and have historical price data."
            print(f"[ERROR] {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)
        
        prices_df = pd.DataFrame(prices_dict)
        print(f"[DEBUG] Created prices dataframe: {prices_df.shape}")
        
        # Fetch NIFTY50 for beta calculation
        print("[DEBUG] Fetching NIFTY 50 data")
        nifty_hist = repo.get_historical_prices('NIFTY 50', days=lookback_days)
        if nifty_hist is None or nifty_hist.empty:
            print("[DEBUG] NIFTY data not found, using portfolio average")
            # Fallback: use average of portfolio stocks
            market_prices = prices_df.mean(axis=1)
        else:
            market_prices = nifty_hist['Close']
            print(f"[DEBUG] Got {len(market_prices)} days of NIFTY data")
        
        # Fetch financials
        print("[DEBUG] Fetching financials")
        financials = []
        for pos in portfolio.positions:
            fs = db.query(FinancialStatement).filter(
                FinancialStatement.company_id == pos.company_id
            ).order_by(desc(FinancialStatement.period_end)).first()
            
            if fs:
                financials.append({
                    'debt_to_equity': fs.debt_to_equity or 0,
                    'roe': fs.roe or 0,
                    'current_ratio': 1.5,  # Placeholder
                    'free_cash_flow': fs.free_cash_flow or 0
                })
            else:
                financials.append({
                    'debt_to_equity': 0,
                    'roe': 0,
                    'current_ratio': 1.0,
                    'free_cash_flow': 0
                })
        
        print(f"[DEBUG] Collected {len(financials)} financial records")
        
        # Run risk analysis
        print("[DEBUG] Initializing PortfolioRiskEngine")
        engine = PortfolioRiskEngine()
        
        print("[DEBUG] Running portfolio analysis")
        analysis = engine.analyze_portfolio(
            prices=prices_df,
            weights=weights,
            market_prices=market_prices,
            financials=financials,
            lookback_days=lookback_days
        )
        
        print("[DEBUG] Analysis complete, caching results")
        
        # Cache results
        for metric_name, metric_value in analysis['market_risk'].items():
            if isinstance(metric_value, (int, float)):
                metric = ComputedRiskMetric(
                    portfolio_id=portfolio_id,
                    metric_name=f"market_{metric_name}",
                    metric_value=float(metric_value)
                )
                db.add(metric)
        
        db.commit()
        print("[DEBUG] Results cached successfully")
        
        # Generate chart data for frontend
        print("[DEBUG] Generating chart data")
        
        # 1. Performance chart data (cumulative returns over time)
        portfolio_daily_returns = (prices_df @ weights).pct_change().fillna(0) * 100
        portfolio_cumulative = portfolio_daily_returns.cumsum()
        
        market_daily_returns = market_prices.pct_change().fillna(0) * 100
        market_cumulative = market_daily_returns.cumsum()
        
        performance_chart = {
            "dates": prices_df.index.strftime('%Y-%m-%d').tolist(),
           "portfolioReturns": portfolio_cumulative.tolist(),
            "benchmarkReturns": market_cumulative.tolist()
        }
        
        # 2. Sector allocation chart data
        sector_allocation = {}
        for pos in portfolio.positions:
            sector = pos.company.sector or "Unknown"
            sector_allocation[sector] = sector_allocation.get(sector, 0) + pos.allocation_pct
        
        sectors_chart = [
            {"name": sector, "allocation": round(alloc, 2)}
            for sector, alloc in sorted(sector_allocation.items(), key=lambda x: -x[1])
        ]
        
        # 3. Risk scatter plot data (volatility vs returns per stock)
        risk_scatter = []
        for i, symbol in enumerate(symbols):
            stock_returns = prices_df[symbol].pct_change().dropna()
            annual_return = stock_returns.mean() * 252 * 100
            annual_volatility = stock_returns.std() * np.sqrt(252) * 100
            
            risk_scatter.append({
                "symbol": symbol,
                "volatility": round(annual_volatility, 2),
                "return": round(annual_return, 2),
                "weight": round(weights[i] * 100, 2)
            })
        
        # Add charts to response
        analysis['charts'] = {
            'performance': performance_chart,
            'sectors': sectors_chart,
            'risk_scatter': risk_scatter
        }
        
        print("[DEBUG] Chart data generated successfully")
        
        # Generate position details with current prices and P&L
        print("[DEBUG] Calculating position details with P&L")
        position_details = []
        
        for pos in portfolio.positions:
            # Get latest price from historical_price table
            latest_price_record = db.query(HistoricalPrice)\
                .filter(HistoricalPrice.company_id == pos.company_id)\
                .order_by(desc(HistoricalPrice.date))\
                .first()
            
            ltp = latest_price_record.close if latest_price_record else None
            last_updated = latest_price_record.date if latest_price_record else None
            
            # Calculate P&L
            invested = pos.invested_value or (pos.quantity * pos.avg_buy_price if pos.quantity and pos.avg_buy_price else 0)
            current_value = (pos.quantity * ltp) if (pos.quantity and ltp) else 0
            pnl = current_value - invested if ltp else None
            pnl_pct = (pnl / invested * 100) if (invested > 0 and pnl is not None) else None
            
            position_details.append({
                "symbol": pos.company.symbol,
                "company_name": pos.company.name,
                "quantity": pos.quantity,
                "avg_buy_price": pos.avg_buy_price,
                "ltp": ltp,
                "last_updated": last_updated.isoformat() if last_updated else None,
                "invested_value": invested,
                "current_value": current_value,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "sector": pos.company.sector or "Unknown",
                "allocation_pct": pos.allocation_pct
            })
        
        analysis['positions'] = position_details
        print(f"[DEBUG] Position details generated for {len(position_details)} positions")
        
        return analysis
        
    except HTTPException as he:
        print(f"[DEBUG] HTTP Exception: {he.detail}")
        db.rollback()
        raise he
    except Exception as e:
        db.rollback()
        import traceback
        print("[DEBUG] Exception occurred:")
        traceback.print_exc()
        print(f"[DEBUG] Exception type: {type(e).__name__}")
        print(f"[DEBUG] Exception message: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e) if str(e) else f"{type(e).__name__}: Unknown error")
    finally:
        db.close()

@app.post("/api/portfolios/{portfolio_id}/monte-carlo")
async def run_monte_carlo(
    portfolio_id: int,
    time_horizon_months: int = 12,
    num_simulations: int = 10000
):
    """Run Monte Carlo simulation for portfolio"""
    db = SessionLocal()
    repo = DataRepository(db)
    
    try:
        # Get portfolio
        portfolio = db.query(UserPortfolio).filter(UserPortfolio.id == portfolio_id).first()
        if not portfolio:
            raise HTTPException(status_code=404, detail="Portfolio not found")
        
        # Get symbols and weights
        symbols = [pos.company.symbol for pos in portfolio.positions]
        weights = np.array([pos.allocation_pct / 100 for pos in portfolio.positions])
        
        # Fetch historical prices
        prices_dict = {}
        for symbol in symbols:
            hist = repo.get_historical_prices(symbol, days=252)
            if hist is None or hist.empty:
                raise HTTPException(status_code=400, detail=f"No historical data for {symbol}")
            prices_dict[symbol] = hist['Close']
        
        prices_df = pd.DataFrame(prices_dict)
        
        # Calculate returns
        engine = PortfolioRiskEngine()
        returns = engine.calculate_returns(prices_df)
        
        # Run Monte Carlo
        time_horizon_days = int(time_horizon_months * 21)  # Approx trading days per month
        results = engine.monte_carlo_simulation(
            returns=returns,
            weights=weights,
            time_horizon_days=time_horizon_days,
            num_simulations=num_simulations
        )
        
        return results
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

# --- Admin / Data Management Endpoints ---

@app.get("/api/admin/sector-stats")
async def get_sector_statistics():
    """Get statistics about sector data population"""
    db = SessionLocal()
    
    try:
        total = db.query(Company).count()
        with_sector = db.query(Company).filter(
            (Company.sector != None) & (Company.sector != '') & (Company.sector != 'Unknown')
        ).count()
        without_sector = total - with_sector
        
        # Get sector breakdown
        from sqlalchemy import func
        sector_counts = db.query(
            Company.sector,
            func.count(Company.id).label('count')
        ).filter(
            (Company.sector != None) & (Company.sector != '') & (Company.sector != 'Unknown')
        ).group_by(Company.sector).all()
        
        sectors = [{'name': s[0], 'count': s[1]} for s in sector_counts]
        sectors.sort(key=lambda x: -x['count'])
        
        return {
            'total_companies': total,
            'with_sector': with_sector,
            'without_sector': without_sector,
            'coverage_pct': round(with_sector / total * 100, 1) if total > 0 else 0,
            'sectors': sectors
        }
    finally:
        db.close()

@app.get("/api/sectors/list")
async def get_sectors_list():
    """Get list of available sectors for filtering"""
    db = SessionLocal()
    
    try:
        from sqlalchemy import distinct
        
        # Get distinct sectors that are not null or empty
        sectors = db.query(distinct(Company.sector)).filter(
            (Company.sector != None) & 
            (Company.sector != '') & 
            (Company.sector != 'Unknown')
        ).order_by(Company.sector).all()
        
        # Extract sector names from tuples
        sector_list = [s[0] for s in sectors]
        
        return {'sectors': sector_list}
    finally:
        db.close()

@app.post("/api/admin/populate-sectors")
async def trigger_sector_population(limit: int = 50):
    """
    Trigger bulk sector population (limited to prevent timeout)
    For full population, use the CLI script: python -m app.populate_sectors
    """
    from .populate_sectors import populate_sectors
    import threading
    
    # Run in background thread to avoid timeout
    def run_population():
        try:
            populate_sectors(limit=limit)
        except Exception as e:
            print(f"Sector population error: {str(e)}")
    
    thread = threading.Thread(target=run_population)
    thread.start()
    
    return {
        'message': f'Sector population started for up to {limit} companies',
        'note': 'Running in background. Check server logs for progress.'
    }

@app.get("/api/sectors/list")
async def list_all_sectors():
    """Get list of all unique sectors in database"""
    db = SessionLocal()
    
    try:
        from sqlalchemy import func, distinct
        sectors = db.query(distinct(Company.sector)).filter(
            (Company.sector != None) & (Company.sector != '') & (Company.sector != 'Unknown')
        ).all()
        
        sector_list = sorted([s[0] for s in sectors if s[0]])
        
        return {'sectors': sector_list, 'count': len(sector_list)}
    finally:
        db.close()


# ==================== STRATEGY BACKTESTING ====================

from .strategies import ORBStrategy
from .strategies.backtest_engine import BacktestEngine, BacktestConfig
from datetime import datetime

class BacktestRequest(BaseModel):
    """Request model for backtesting"""
    strategy_name: str
    symbol: str
    start_date: str  # YYYY-MM-DD
    end_date: str  # YYYY-MM-DD
    timeframe: str = "5min"  # 1min, 5min, 15min, 1D
    initial_capital: float = 100000
    params: Optional[Dict[str, Any]] = {}

@app.get("/api/strategies/list")
async def list_strategies():
    """List all available trading strategies"""
    strategies = [
        {
            "name": "ORB",
            "display_name": "Opening Range Breakout",
            "description": "Trades breakouts from opening range with CE/PE executions",
            "timeframes": ["1min", "5min", "15min"],
            "default_params": {
                "opening_range_minutes": 5,
                "stop_loss_pct": 0.5,
                "take_profit_pct": 1.5,
                "max_positions_per_day": 1,
                "trade_type": "options"
            }
        }
    ]
    
    return {"strategies": strategies}

@app.post("/api/strategies/backtest")
async def run_backtest(request: BacktestRequest):
    """
    Run backtest for a strategy
    
    For intraday strategies (5min, 15min):
    - Uses equity data simulation (CE/PE not available yet)
    - Simulates directional trades based on strategy signals
    """
    if not request.symbol or request.symbol.strip() == "":
        raise HTTPException(status_code=400, detail="Symbol is required for backtesting")
        
    db = SessionLocal()
    repo = DataRepository(db)
    
    try:
        print(f"[BACKTEST] Starting backtest for {request.strategy_name} on {request.symbol}")
        
        # Validate strategy
        if request.strategy_name not in ['ORB']:
            raise HTTPException(status_code=400, detail=f"Strategy {request.strategy_name} not found")
        
        # Parse dates
        start_date = datetime.strptime(request.start_date, "%Y-%m-%d")
        end_date = datetime.strptime(request.end_date, "%Y-%m-%d")
        
        # Fetch data based on timeframe
        if request.timeframe in ['1min', '5min', '15min', '30min', '60min']:
            # Use intraday data
            timeframe_map = {'1min': 1, '5min': 5, '15min': 15, '30min': 30, '60min': 60}
            tf_minutes = timeframe_map.get(request.timeframe, 5)
            
            print(f"[BACKTEST] Fetching {tf_minutes}-minute intraday data for {request.symbol}")
            hist = repo.get_intraday_candles(
                symbol=request.symbol,
                timeframe=tf_minutes,
                start_date=start_date,
                end_date=end_date
            )
            
            if hist is None or hist.empty:
                # Fall back to daily data if intraday not available
                print(f"[BACKTEST] No intraday data found, falling back to daily data")
                days = (end_date - start_date).days + 30
                hist = repo.get_historical_prices(request.symbol, days=days)
                
                if hist is None or hist.empty:
                    raise HTTPException(
                        status_code=400,
                        detail=f"No data found for {request.symbol}. Please ensure intraday data is available."
                    )
                
                # Add timestamp column for daily data
                if 'timestamp' not in hist.columns:
                    hist = hist.reset_index()
                    # repository returns index named 'date' (lowercase), so reset_index creates 'date' col
                    if 'date' in hist.columns:
                        hist['timestamp'] = pd.to_datetime(hist['date'])
                    elif 'Date' in hist.columns:
                        hist['timestamp'] = pd.to_datetime(hist['Date'])
                    else:
                        # Fallback if index name was lost or different
                        hist['timestamp'] = pd.to_datetime(hist.iloc[:, 0])
                
                # Filter to requested date range
                hist = hist[
                    (hist['timestamp'] >= start_date) & 
                    (hist['timestamp'] <= end_date)
                ].copy()
                
                # Normalize column names
                hist.columns = hist.columns.str.lower()
        else:
            # Use daily data for 1D timeframe
            days = (end_date - start_date).days + 30
            
            print(f"[BACKTEST] Fetching daily data for {request.symbol}")
            hist = repo.get_historical_prices(request.symbol, days=days)
            
            if hist is None or hist.empty:
                raise HTTPException(
                    status_code=400, 
                    detail=f"No historical data found for {request.symbol}"
                )
            
            # Add timestamp column if using Date index
            if 'timestamp' not in hist.columns:
                hist = hist.reset_index()
                # Check for 'date' or 'Date' or fallback
                if 'date' in hist.columns:
                    hist['timestamp'] = pd.to_datetime(hist['date'])
                elif 'Date' in hist.columns:
                    hist['timestamp'] = pd.to_datetime(hist['Date'])
                else:
                    hist['timestamp'] = pd.to_datetime(hist.iloc[:, 0])
            
            # Filter to requested date range
            hist = hist[
                (hist['timestamp'] >= start_date) & 
                (hist['timestamp'] <= end_date)
            ].copy()
            
            # Normalize column names
            hist.columns = hist.columns.str.lower()
        
        print(f"[BACKTEST] Prepared {len(hist)} candles for backtest")

        
        if len(hist) == 0:
            raise HTTPException(
                status_code=400,
                detail=f"No data available for {request.symbol} in the specified date range"
            )
        
        # Normalize column names (handle both 'Close' and 'close')
        hist.columns = hist.columns.str.lower()
        required_cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
        
        # Check if all required columns exist
        missing_cols = [col for col in required_cols if col not in hist.columns]
        if missing_cols:
            raise HTTPException(
                status_code=500,
                detail=f"Missing required columns: {missing_cols}"
            )
        
        # Initialize strategy
        if request.strategy_name == 'ORB':
            strategy_params = {
                'symbol': request.symbol,
                'opening_range_minutes': request.params.get('opening_range_minutes', 5),
                'stop_loss_atr_multiplier': request.params.get('stopLoss', 0.5),  # ATR multiplier from UI
                'take_profit_atr_multiplier': request.params.get('takeProfit', 1.5),  # ATR multiplier from UI
                'max_positions_per_day': request.params.get('max_positions_per_day', 1),
                'trade_type': request.params.get('trade_type', 'options')
            }
            strategy = ORBStrategy(strategy_params)
        else:
            raise HTTPException(status_code=400, detail="Strategy not implemented")
        
        # Initialize backtest config with risk_per_trade from frontend
        config = BacktestConfig(
            initial_capital=request.initial_capital,
            commission_pct=0.03,  # 0.03%
            slippage_pct=0.05,    # 0.05%
            max_positions=1,
            risk_per_trade_pct=request.params.get('riskPerTrade', 2.0)  # Use frontend value
        )
        
        # Run backtest
        print(f"[BACKTEST] Running backtest...")
        engine = BacktestEngine(strategy, config)
        results = engine.run(hist, request.symbol)
        
        print(f"[BACKTEST] Backtest complete. Total trades: {results['summary']['total_trades']}")
        
        return {
            "status": "success",
            "strategy": request.strategy_name,
            "symbol": request.symbol,
            "period": {
                "start": request.start_date,
                "end": request.end_date,
                "days": (end_date - start_date).days
            },
            **results
        }
        
    except HTTPException as he:
        raise he
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/strategies/{strategy_name}/params")
async def get_strategy_params(strategy_name: str):
    """Get default parameters for a strategy"""
    params_map = {
        "ORB": {
            "opening_range_minutes": {
                "type": "number",
                "default": 5,
                "min": 1,
                "max": 30,
                "description": "Opening range duration in minutes"
            },
            "stop_loss_pct": {
                "type": "number",
                "default": 0.5,
                "min": 0.1,
                "max": 5.0,
                "step": 0.1,
                "description": "Stop loss percentage"
            },
            "take_profit_pct": {
                "type": "number",
                "default": 1.5,
                "min": 0.5,
                "max": 10.0,
                "step": 0.1,
                "description": "Take profit percentage"
            },
            "max_positions_per_day": {
                "type": "number",
                "default": 1,
                "min": 1,
                "max": 5,
                "description": "Maximum trades per day"
            },
            "trade_type": {
                "type": "select",
                "default": "options",
                "options": ["options", "equity"],
                "description": "Trade using options (CE/PE) or equity"
            }
        }
    }
    
    if strategy_name not in params_map:
        raise HTTPException(status_code=404, detail="Strategy not found")
    
    return {"params": params_map[strategy_name]}


# ============================================================================
# SMART TRADER API ENDPOINTS - Terminal Integration
# ============================================================================

# Global orchestrator instance
smart_trader_orchestrator = None

@app.post("/api/smart-trader/start")
async def start_smart_trader():
    """Start the Smart Trader multi-agent system"""
    global smart_trader_orchestrator
    
    try:
        if smart_trader_orchestrator is None:
            from .smart_trader.orchestrator import get_orchestrator
            smart_trader_orchestrator = get_orchestrator()
        
        result = await smart_trader_orchestrator.start_market_session()
        return result
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to start Smart Trader: {str(e)}")


@app.post("/api/smart-trader/stop")
async def stop_smart_trader():
    """Stop the Smart Trader system"""
    global smart_trader_orchestrator
    
    if smart_trader_orchestrator:
        result = smart_trader_orchestrator.stop_market_session()
        return result
    
    return {"status": "not_running"}


@app.get("/api/smart-trader/status")
async def get_smart_trader_status():
    """Get Smart Trader system status"""
    global smart_trader_orchestrator
    
    if smart_trader_orchestrator:
        return smart_trader_orchestrator.get_system_state()
    
    return {
        "running": False,
        "message": "Smart Trader not started"
    }


@app.get("/api/smart-trader/positions")
async def get_smart_trader_positions():
    """Get all open positions from Smart Trader agents"""
    global smart_trader_orchestrator
    
    if smart_trader_orchestrator and smart_trader_orchestrator.execution_agent:
        positions = smart_trader_orchestrator.execution_agent.get_open_positions()
        return {"positions": positions}
    
    return {"positions": []}


@app.get("/api/smart-trader/tradebook")
async def get_smart_trader_tradebook(limit: int = 50):
    """Get trade history from Smart Trader"""
    global smart_trader_orchestrator
    
    if smart_trader_orchestrator and smart_trader_orchestrator.journal_agent:
        trades = smart_trader_orchestrator.journal_agent.get_tradebook(limit=limit)
        return {"trades": trades}
    
    return {"trades": []}


@app.get("/api/smart-trader/pnl")
async def get_smart_trader_pnl():
    """Get P&L summary from Smart Trader"""
    global smart_trader_orchestrator
    
    if smart_trader_orchestrator and smart_trader_orchestrator.journal_agent:
        pnl = smart_trader_orchestrator.journal_agent.get_pnl_summary()
        return pnl
    
    return {
        "total_pnl": 0,
        "total_trades": 0,
        "win_rate": 0,
        "profit_factor": 0
    }


@app.post("/api/smart-trader/close-position")
async def close_smart_trader_position(trade_id: str):
    """Close a specific Smart Trader position"""
    global smart_trader_orchestrator
    
    if not smart_trader_orchestrator or not smart_trader_orchestrator.execution_agent:
        raise HTTPException(status_code=400, detail="Smart Trader not running")
    
    # Find the position
    positions = smart_trader_orchestrator.execution_agent.get_open_positions()
    position = next((p for p in positions if p.get('trade_id') == trade_id), None)
    
    if not position:
        raise HTTPException(status_code=404, detail="Position not found")
    
    # Get current price (use position's current_price or fetch live)
    current_price = position.get('current_price', position.get('entry_price'))
    
    # Close the position
    result = smart_trader_orchestrator.execution_agent.close_position(
        trade_id, 
        current_price, 
        'Manual Close from Terminal'
    )
    
    return result


@app.get("/api/smart-trader/signals")
async def get_smart_trader_signals():
    """Get current trading signals"""
    global smart_trader_orchestrator
    
    if smart_trader_orchestrator:
        return smart_trader_orchestrator.get_current_signals()
    
    return {"signals": []}


@app.post("/api/smart-trader/scan")
async def trigger_manual_scan():
    """Manually trigger a scan cycle"""
    global smart_trader_orchestrator
    
    if smart_trader_orchestrator and smart_trader_orchestrator.is_running:
        smart_trader_orchestrator.trigger_scan_cycle()
        return {"status": "success", "message": "Scan triggered"}
    
    return {"status": "error", "message": "Smart Trader not running"}


@app.get("/api/screener/trending")
async def get_trending_stocks(
    filter_type: str = 'ALL',
    limit: int = 50,
    page: int = 1,
    sort_by: str = None,
    sort_order: str = 'desc',
    symbol: Optional[str] = None,
    sector: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """
    Get trending stocks based on real-time calculations
    Independent of Smart Trader scanner - uses historical data + live prices
    
    Args:
        filter_type: One of 'VOLUME_SHOCKER', 'PRICE_SHOCKER', '52W_HIGH', '52W_LOW', 'ALL'
        limit: Maximum number of results (default 50)
        sort_by: Field to sort by (e.g., 'change_pct', 'close')
        sort_order: 'asc' or 'desc'
        symbol: Filter by symbol substring
        sector: Filter by sector exact match
    """
    from .trending_scanner import calculate_trending_stocks
    
    try:
        stocks, total_count = calculate_trending_stocks(
            db, 
            filter_type, 
            limit, 
            page, 
            sort_by, 
            sort_order,
            symbol,
            sector
        )
        
        # Calculate total pages
        import math
        total_pages = math.ceil(total_count / limit) if limit > 0 else 1
        
        return {
            "data": stocks,
            "meta": {
                "page": page,
                "limit": limit,
                "total": total_count,
                "total_pages": total_pages
            }
        }
    except Exception as e:
        print(f"Error calculating trending stocks: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/smart-trader/execute-trade")
async def execute_smart_trader_trade(signal_id: str, quantity: Optional[int] = None):
    """Execute a trade from a signal"""
    global smart_trader_orchestrator
    
    if not smart_trader_orchestrator:
        raise HTTPException(status_code=400, detail="Smart Trader not running")
    
    result = await smart_trader_orchestrator.execute_signal(signal_id, quantity)
    return result


# ============================================================================
# AI COPILOT API ENDPOINTS - Natural Language Stock Screening
# ============================================================================

from pydantic import BaseModel

class CopilotQuery(BaseModel):
    query: str
    limit: Optional[int] = 20

@app.post("/api/copilot/generate-list")
async def generate_stock_list_copilot(request: CopilotQuery):
    """
    Generate stock list from natural language query
    
    Example queries:
    - "Find oversold stocks with RSI < 30"
    - "High momentum IT stocks"
    - "Undervalued stocks with strong fundamentals"
    """
    try:
        print(f"🔍 AI Copilot query received: {request.query}")
        from .smart_trader.llm_copilot_agent import LLMCopilotAgent
        
        db = SessionLocal()
        try:
            print(f"📊 Initializing LLMCopilotAgent with provider=groq")
            copilot = LLMCopilotAgent(db_session=db, provider="groq")
            print(f"✅ LLMCopilotAgent initialized successfully")
            
            print(f"🚀 Generating stock list for query: {request.query}")
            result = await copilot.generate_stock_list(
                user_query=request.query,
                limit=request.limit
            )
            print(f"✅ Stock list generated: {len(result.get('stocks', []))} stocks found")
            return result
        finally:
            db.close()
    
    except Exception as e:
        print(f"❌ Copilot failed: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Copilot failed: {str(e)}")


@app.get("/api/copilot/suggestions")
async def get_copilot_suggestions():
    """Get suggested queries for AI Copilot"""
    try:
        from .smart_trader.llm_copilot_agent import LLMCopilotAgent
        
        db = SessionLocal()
        try:
            copilot = LLMCopilotAgent(db_session=db, provider="groq")
            suggestions = await copilot.suggest_queries()
            return {"suggestions": suggestions}
        finally:
            db.close()
    
    except Exception as e:
        # Return default suggestions if LLM fails
        return {
            "suggestions": [
                "Find oversold stocks with RSI < 30",
                "High momentum stocks in IT sector",
                "Undervalued stocks with PE < 15 and ROE > 20",
                "Stocks breaking out with high volume",
                "Low debt companies with strong fundamentals"
            ]
        }


@app.get("/api/symbols/search")
async def search_symbols(q: str, limit: int = 10, db: Session = Depends(get_db)):
    """Search for stock symbols"""
    if not q:
        return []
    
    q = q.upper()
    
    results = db.query(Company).filter(
        and_(
            Company.is_active == True,
            or_(
                Company.symbol.like(f"{q}%"),  # Starts with
                Company.name.ilike(f"%{q}%")   # Contains name
            )
        )
    ).limit(limit).all()
    
    return {
        "symbols": [
            {
                "symbol": c.symbol,
                "name": c.name,
                "sector": c.sector
            }
            for c in results
        ]
    }

# ==================== TRADING & SIGNALS API ====================

@app.get("/api/signals")
async def get_signals(limit: int = 50):
    """Get recent trading signals"""
    try:
        from .smart_trader.signal_history import get_signal_history_service
        service = get_signal_history_service()
        signals = service.get_all_signals(limit=limit)
        return {"signals": signals}
    except Exception as e:
        print(f"Error fetching signals: {e}")
        return {"signals": []}

@app.post("/api/trading/paper/order")
async def place_paper_order(order: dict):
    """Place a manual paper trade"""
    try:
        from .smart_trader.new_orchestrator import get_orchestrator
        orchestrator = get_orchestrator()
        
        # Ensure execution agent is initialized
        if not orchestrator.execution_agent:
             return JSONResponse(status_code=500, content={"error": "Execution agent not initialized"})

        result = orchestrator.execution_agent.execute_manual_trade({
            'symbol': order.get('symbol'),
            'type': order.get('type'), # BUY/SELL
            'quantity': int(order.get('quantity', 1)),
            'price': float(order.get('price', 0)),
            'instrument_type': order.get('instrument_type', 'EQ')
        })
        
        return result
    except Exception as e:
        print(f"Error placing paper order: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


# ==================== NEWS API ENDPOINTS ====================

@app.get("/api/news/latest")
async def get_latest_news(limit: int = 50):
    """Get latest market news from multiple Indian sources"""
    try:
        import feedparser
        from datetime import datetime
        
        all_news = []
        
        # Multiple Indian news sources
        sources = [
            {
                "name": "Economic Times",
                "url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"
            },
            {
                "name": "Moneycontrol",
                "url": "https://www.moneycontrol.com/rss/marketreports.xml"
            },
            {
                "name": "Business Standard",
                "url": "https://www.business-standard.com/rss/markets-106.rss"
            },
            {
                "name": "Google News",
                "url": "https://news.google.com/rss/search?q=Indian+stock+market+NSE+BSE&hl=en-IN&gl=IN&ceid=IN:en"
            }
        ]
        
        # Fetch from all sources
        for source in sources:
            try:
                feed = feedparser.parse(source["url"])
                
                for entry in feed.entries[:15]:  # 15 from each source
                    # Extract symbols from title
                    symbols = []
                    title_upper = entry.title.upper()
                    common_symbols = ['RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 
                                    'HINDUNILVR', 'ITC', 'SBIN', 'BHARTIARTL', 'KOTAKBANK',
                                    'NIFTY', 'SENSEX', 'BANKNIFTY']
                    for symbol in common_symbols:
                        if symbol in title_upper:
                            symbols.append(symbol)
                    
                    all_news.append({
                        "id": hash(entry.link),
                        "title": entry.title,
                        "summary": entry.get('summary', entry.title)[:200],  # Truncate summary
                        "source": source["name"],
                        "url": entry.link,
                        "published_at": datetime(*entry.published_parsed[:6]).replace(tzinfo=timezone.utc).isoformat() if hasattr(entry, 'published_parsed') else datetime.now(timezone.utc).isoformat(),
                        "symbols": symbols,
                        "sentiment": "neutral"
                    })
            except Exception as e:
                print(f"Error fetching from {source['name']}: {e}")
                continue
        
        # Sort by published date (newest first)
        all_news.sort(key=lambda x: x['published_at'], reverse=True)
        
        return {
            "success": True,
            "count": len(all_news[:limit]),
            "news": all_news[:limit]
        }
    except Exception as e:
        print(f"Error fetching news: {e}")
        return {
            "success": False,
            "count": 0,
            "news": [],
            "error": str(e)
        }


@app.get("/api/news/by-symbol/{symbol}")
async def get_news_by_symbol(symbol: str, limit: int = 20):
    """Get news for a specific stock symbol"""
    try:
        import feedparser
        from datetime import datetime
        
        # Fetch from Google News with symbol query
        query = f"{symbol}+stock+India"
        rss_url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
        feed = feedparser.parse(rss_url)
        
        news = []
        for entry in feed.entries[:limit]:
            news.append({
                "id": hash(entry.link),
                "title": entry.title,
                "summary": entry.get('summary', entry.title),
                "source": "Google News",
                "url": entry.link,
                "published_at": datetime(*entry.published_parsed[:6]).isoformat() if hasattr(entry, 'published_parsed') else datetime.utcnow().isoformat(),
                "symbols": [symbol.upper()],
                "sentiment": "neutral"
            })
        
        return {
            "success": True,
            "symbol": symbol.upper(),
            "count": len(news),
            "news": news
        }
    except Exception as e:
        print(f"Error fetching news: {e}")
        return {
            "success": False,
            "count": 0,
            "news": [],
            "error": str(e)
        }



if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
