from fastapi import FastAPI, HTTPException, WebSocket, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, desc, func
from sqlalchemy.orm import sessionmaker
from typing import List, Optional
import json
import numpy as np
from datetime import date
from .database import Base, Company, FinancialStatement, HistoricalPrice, engine, SessionLocal, UserPortfolio, PortfolioPosition, ComputedRiskMetric
from .data_repository import DataRepository
from .portfolio_risk import PortfolioRiskEngine
import pandas as pd

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Start Cache Manager
# Initialize Database
Base.metadata.create_all(bind=engine)

# --- Compute Features (EMA, RSI, etc) ---
import pandas as pd
from .indicators import compute_features

# --- Cache for Screeners ---
SCREENER_CACHE = {
    'last_update': None,
    'data': []
}

@app.get("/")
def read_root():
    return {"status": "ok", "message": "AlgoTrading Backend Running"}

@app.get("/api/symbols/search")
async def search_symbols(q: str = ""):
    if len(q) < 1:
        return {"symbols": []}
    
    from .database import SessionLocal, Company
    db = SessionLocal()
    try:
        companies = db.query(Company).filter(
            Company.symbol.ilike(f"{q}%")
        ).order_by(Company.symbol).limit(10).all()
        
        return {
            "symbols": [
                {"symbol": c.symbol, "name": c.name} 
                for c in companies
            ]
        }
    finally:
        db.close()

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
        
        # Add symbol filter if provided
        if symbol:
            companies_query = companies_query.filter(Company.symbol == symbol.upper())
        
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
        else:
            # Default to symbol sorting
            companies_query = companies_query.order_by(Company.symbol)
        
        total_records = companies_query.count()
        
        # Paginate
        offset = (page - 1) * limit
        results = companies_query.offset(offset).limit(limit).all()
        
        # Compute features for the paginated results only
        computed_list = []
        for company, hist_price in results:
            try:
                hist = repo.get_historical_prices(company.symbol, days=200)
                if hist is not None and not hist.empty:
                    features = compute_features(company.symbol, hist)
                else:
                    # Use the latest price from the join
                    features = {
                        'symbol': company.symbol,
                        'close': float(hist_price.close),
                        'volume': int(hist_price.volume),
                        'ema20': 0,
                        'ema50': 0,
                        'atr_pct': 0,
                        'rsi': 0,
                        'vol_percentile': 0
                    }
                
                if 'symbol' not in features:
                    features['symbol'] = company.symbol
                computed_list.append(features)
            except Exception as e:
                # Fallback to basic data
                computed_list.append({
                    'symbol': company.symbol,
                    'close': float(hist_price.close),
                    'volume': int(hist_price.volume),
                    'ema20': 0,
                    'ema50': 0,
                    'atr_pct': 0,
                    'rsi': 0,
                    'vol_percentile': 0
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
        for symbol in symbols:
            print(f"[DEBUG] Fetching historical data for {symbol}")
            hist = repo.get_historical_prices(symbol, days=lookback_days)
            if hist is None or hist.empty:
                raise HTTPException(status_code=400, detail=f"No historical data for {symbol}")
            prices_dict[symbol] = hist['Close']
            print(f"[DEBUG] Got {len(hist)} days of data for {symbol}")
        
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
