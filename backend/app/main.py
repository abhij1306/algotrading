"""
FastAPI main application
NSE Intraday/Swing Trading Screener
"""
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
import time
from .config import config
from .screener import run_screener
from .data_fetcher import fetch_yfinance_data
from .models import ScreenerResponse, HealthResponse
from .cache_api import router as cache_router

app = FastAPI(
    title="NSE Trading Screener",
    description="Intraday and Swing trading stock screener for NSE F&O stocks",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include cache management routes
app.include_router(cache_router, prefix="/api", tags=["cache"])

# In-memory cache
CACHE = {
    'screener': None,
    'generated_at': None,
    'cache_time': 0
}

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "NSE Trading Screener API",
        "version": "1.0.0",
        "mode": config.get_mode(),
        "endpoints": {
            "screener": "/api/screener",
            "history": "/api/history/{symbol}",
            "health": "/api/health"
        }
    }

@app.get("/api/health", response_model=HealthResponse)
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "mode": config.get_mode(),
        "has_fyers": config.HAS_FYERS
    }

@app.get("/api/strategies")
async def get_strategies():
    """Get available trading strategies"""
    from .strategies import get_all_strategies
    return {
        "strategies": get_all_strategies()
    }

@app.get("/api/market-data")
async def get_market_data_api():
    """Get market data (FII/DII activity)"""
    from .market_data import get_market_data
    return get_market_data()

@app.get("/api/screener")
async def get_screener(strategy: str = 'momentum'):
    """
    Main screener endpoint with strategy selection
    Returns stocks based on selected strategy
    """
    # Check cache
    cache_key = f'screener_{strategy}'
    current_time = time.time()
    if cache_key in CACHE and (current_time - CACHE[cache_key].get('cache_time', 0)) < config.CACHE_TTL:
    
    start_time = time.time()
    
    try:
        from .strategies import calculate_score_with_strategy, get_strategy
        from .database import SessionLocal
        from .data_repository import DataRepository
        from .indicators import compute_features
        from .data_fetcher import fetch_fyers_quotes
        
        # Get strategy config
        strategy_config = get_strategy(strategy)
        
        # Get database session
        db = SessionLocal()
        repo = DataRepository(db)
        
        # Get all F&O companies
        companies = repo.get_all_companies(fno_only=True, active_only=True)
        universe = [c.symbol for c in companies]
        
        
        # Compute features for all stocks
        
        # Compute features for all stocks
        all_features = []
        for symbol in universe:
            try:
                # Get historical data from database
                hist = repo.get_historical_prices(symbol, days=365)
                if hist is None or hist.empty:
                    continue
                
                # Compute features
                features = compute_features(symbol, hist)
                if features:
                    all_features.append(features)
            except Exception as e:
                print(f"Error processing {symbol}: {e}")
                continue
        
        # Get real-time quotes if Fyers is available
        if config.HAS_FYERS and all_features:
            try:
                symbols = [f['symbol'] for f in all_features]
                quotes = fetch_fyers_quotes(symbols)
                
                # Update latest prices
                for features in all_features:
                    symbol = features['symbol']
                    if symbol in quotes:
                        quote = quotes[symbol]
                        features['close'] = quote['ltp']
                        features['volume'] = quote.get('volume', features.get('volume', 0))
                        features['data_source'] = 'fyers'
            except Exception as e:
                print(f"Failed to fetch real-time quotes: {e}")
        
        # Calculate scores using selected strategy
        for features in all_features:
            score = calculate_score_with_strategy(features, strategy)
            features['score'] = score
        
        # Filter and sort by score
        stocks = [f for f in all_features if f.get('score', 0) > 0]
        stocks.sort(key=lambda x: x.get('score', 0), reverse=True)
        
        # Limit to top 50
        stocks = stocks[:50]
        
        # Build response
        response = {
            'stocks': stocks,
            'stats': {
                'total_screened': len(universe),
                'features_computed': len(all_features),
                'stock_count': len(stocks)
            },
            'generated_at': datetime.utcnow().isoformat() + 'Z',
            'mode': config.get_mode(),
            'strategy': strategy,
            'processing_time_seconds': round(time.time() - start_time, 2)
        }
        
        # Update cache
        CACHE[cache_key] = {
            'data': response,
            'cache_time': current_time
        }
        
        
        db.close()
        return response
        
        db.close()
        return response
        
    except Exception as e:
        print(f"Error in screener: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Screening failed: {str(e)}")

@app.get("/api/history/{symbol}")
async def get_history(symbol: str, days: int = 400):
    """
    Get historical data for a symbol
    
    Args:
        symbol: Stock symbol (without .NS)
        days: Number of days (default: 400)
    """
    try:
        from .data_fetcher import fetch_historical_data
        
        # Use our robust fetcher (DB -> Fyers -> yfinance)
        hist = fetch_historical_data(symbol, days=days)
        
        if hist is None or hist.empty:
            raise HTTPException(status_code=404, detail=f"No data found for {symbol}")
        
        # Reset index to get Date column if it's the index
        if 'Date' not in hist.columns and hist.index.name == 'Date':
            hist = hist.reset_index()
            
        # Ensure Date format
        if 'Date' in hist.columns:
            hist['Date'] = pd.to_datetime(hist['Date']).dt.strftime('%Y-%m-%d')
        
        return {
            'symbol': symbol,
            'history': hist.tail(days).to_dict(orient='records')
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching history: {str(e)}")

@app.get("/api/fundamentals/{symbol}")
async def get_fundamentals(symbol: str):
    """
    Get fundamental data for a symbol
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol + ".NS")
        info = ticker.info
        return {
            "symbol": symbol,
            "sector": info.get("sector", "Unknown"),
            "industry": info.get("industry", "Unknown"),
            "marketCap": info.get("marketCap", 0),
            "peRatio": info.get("trailingPE", 0),
            "bookValue": info.get("bookValue", 0),
            "dividendYield": info.get("dividendYield", 0),
        }
    except Exception as e:
        print(f"Error fetching fundamentals for {symbol}: {e}")
        return {
            "symbol": symbol,
            "error": str(e)
        }

@app.post("/api/upload/financials")
async def upload_financials(file: UploadFile = File(...), symbol: str = Form(...)):
    """
    Upload Screener.in Excel for fundamentals
    """
    try:
        content = await file.read()
        
        # Parse Excel
        from .excel_parser import parse_screener_excel
        data = parse_screener_excel(content)
        
        if not data:
            # Parser returned no data (not implemented or format not recognized)
            # Still return success so user knows file was received
            return {
                "message": f"File uploaded for {symbol}, but no financial data extracted. Excel parser needs implementation for this format.",
                "symbol": symbol,
                "records_saved": 0
            }
             
        # Save to DB
        from .database import SessionLocal
        from .data_repository import DataRepository
        
        db = SessionLocal()
        repo = DataRepository(db)
        
        count = 0
        for record in data:
            repo.save_financial_statement(symbol, record)
            count += 1
            
        db.close()
        
        return {"message": f"Successfully saved {count} financial records for {symbol}"}
        
    except Exception as e:
        print(f"Upload error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/symbols")
async def get_symbols():
    """Get all available symbols"""
    try:
        from .database import SessionLocal
        from .data_repository import DataRepository
        
        db = SessionLocal()
        repo = DataRepository(db)
        companies = repo.get_all_companies(fno_only=True, active_only=True)
        db.close()
        
        return {"symbols": [c.symbol for c in companies]}
    except Exception as e:
        return {"symbols": [], "error": str(e)}

def sanitize_for_json(obj):
    """Replace NaN and inf values with safe defaults"""
    import math
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_for_json(item) for item in obj]
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return 0.0
        return obj
    return obj

@app.post("/api/risk/comprehensive")
async def comprehensive_risk_assessment(request: dict):
    """
    Comprehensive risk assessment combining technical and fundamental analysis
    Request: {symbols: [...], allocations: [...], lookback_days: 365}
    """
    try:
        from .database import SessionLocal
        from .data_repository import DataRepository
        from .risk_metrics import RiskMetricsEngine
        from .data_fetcher import fetch_historical_data
        import pandas as pd
        import numpy as np
        
        symbols = request.get('symbols', [])
        allocations = request.get('allocations', [])
        lookback_days = request.get('lookback_days', 365)
        
        if not symbols:
            raise HTTPException(status_code=400, detail="No symbols provided")
        
        # Normalize allocations
        if not allocations or len(allocations) != len(symbols):
            allocations = [100.0 / len(symbols)] * len(symbols)
        
        total_alloc = sum(allocations)
        allocations = [a / total_alloc for a in allocations]
        
        db = SessionLocal()
        repo = DataRepository(db)
        engine = RiskMetricsEngine()
        
        position_metrics = []
        all_warnings = []
        portfolio_returns = None
        
        # Calculate NIFTY returns once for beta calculation
        nifty_returns = None
        nifty_data = repo.get_historical_prices("NIFTY", days=lookback_days)
        if not nifty_data.empty:
            nifty_df = pd.DataFrame(nifty_data)
            nifty_df.columns = [c.lower() for c in nifty_df.columns]
            if 'close' in nifty_df.columns:
                nifty_returns = engine.calculate_returns(nifty_df['close'])
        
        for i, symbol in enumerate(symbols):
            # Fetch price data using robust fetcher
            price_data = fetch_historical_data(symbol, days=lookback_days)
            
            if price_data is None or price_data.empty:
                continue
            
            # Normalize column names to lowercase
            price_data.columns = [c.lower() for c in price_data.columns]
            
            if 'close' not in price_data.columns:
                continue
            
            prices = price_data['close']
            returns = engine.calculate_returns(prices)
            
            # Calculate technical metrics
            tech_metrics = {
                'sharpe_ratio': engine.sharpe_ratio(returns),
                'sortino_ratio': engine.sortino_ratio(returns),
                'max_drawdown': engine.max_drawdown(prices),
                'var_95': engine.value_at_risk(returns, 0.95),
                'var_99': engine.value_at_risk(returns, 0.99),
                'cvar_95': engine.conditional_var(returns, 0.95),
                'volatility': engine.annualized_volatility(returns),
                'beta': engine.beta(returns, nifty_returns) if isinstance(nifty_returns, pd.Series) and len(nifty_returns) > 0 else 1.0
            }
            
            # Fetch fundamental data
            financials = repo.get_latest_financials(symbol)
            fund_metrics = {}
            
            if financials:
                # Calculate fundamental metrics from REAL data only
                total_debt = (financials.total_debt or 0)
                equity = (financials.shareholders_equity or 0)
                
                fund_metrics = {
                    'debt_equity': engine.debt_to_equity(total_debt, equity) if equity > 0 else 0,
                    'roe': engine.roe(financials.net_income or 0, equity) if equity > 0 else 0,
                    'roa': engine.roa(financials.net_income or 0, financials.total_assets or 0),
                    'profit_margin': engine.profit_margin(financials.net_income or 0, financials.revenue or 0),
                    'current_ratio': engine.current_ratio(
                        financials.total_assets or 0,  # Approximation
                        financials.total_liabilities or 0
                    ),
                    'interest_coverage': 999.0
                }
                
                # Calculate risk scores
                tech_score = engine.technical_risk_score(tech_metrics)
                fund_score = engine.fundamental_risk_score(fund_metrics)
                combined_score, grade = engine.combined_risk_score(tech_score, fund_score)
            else:
                # NO mock data - use technical analysis ONLY
                fund_metrics = {}
                tech_score = engine.technical_risk_score(tech_metrics)
                fund_score = 5.0  # Neutral score when no data
                combined_score = tech_score  # Technical only
                grade = engine._score_to_grade(combined_score)
            
            # Generate warnings
            warnings = engine.generate_warnings(symbol, tech_metrics, fund_metrics)
            all_warnings.extend(warnings)
            
            # Build position metrics
            position_metrics.append({
                'symbol': symbol,
                'allocation': allocations[i],
                'technical_score': round(tech_score, 2),
                'fundamental_score': round(fund_score, 2),
                'combined_score': round(combined_score, 2),
                'risk_grade': grade,
                **{k: round(v, 4) if isinstance(v, (int, float)) else v 
                   for k, v in tech_metrics.items()},
                **{k: round(v, 4) if isinstance(v, (int, float)) else v 
                   for k, v in fund_metrics.items()}
            })
            
            # Accumulate portfolio returns
            if not isinstance(portfolio_returns, pd.Series):
                portfolio_returns = returns * allocations[i]
            else:
                portfolio_returns = portfolio_returns + (returns * allocations[i])
        
        # Calculate portfolio-level metrics
        if isinstance(portfolio_returns, pd.Series) and len(portfolio_returns) > 0:
            portfolio_tech = {
                'sharpe_ratio': engine.sharpe_ratio(portfolio_returns),
                'sortino_ratio': engine.sortino_ratio(portfolio_returns),
                'var_95': engine.value_at_risk(portfolio_returns, 0.95),
                'var_99': engine.value_at_risk(portfolio_returns, 0.99),
                'cvar_95': engine.conditional_var(portfolio_returns, 0.95),
                'volatility': engine.annualized_volatility(portfolio_returns),
                'beta': engine.beta(portfolio_returns, nifty_returns) if isinstance(nifty_returns, pd.Series) and len(nifty_returns) > 0 else 1.0
            }
            
            # Average fundamental scores
            avg_fund_score = np.mean([p['fundamental_score'] for p in position_metrics])
            avg_tech_score = np.mean([p['technical_score'] for p in position_metrics])
            
            portfolio_combined, portfolio_grade = engine.combined_risk_score(avg_tech_score, avg_fund_score)
            
            portfolio_metrics = {
                'technical_risk_score': round(avg_tech_score, 2),
                'fundamental_risk_score': round(avg_fund_score, 2),
                'combined_risk_score': round(portfolio_combined, 2),
                'risk_grade': portfolio_grade,
                **{k: round(v, 4) if isinstance(v, (int, float)) else v 
                   for k, v in portfolio_tech.items()}
            }
        else:
            portfolio_metrics = {
                'technical_risk_score': 5.0,
                'fundamental_risk_score': 5.0,
                'combined_risk_score': 5.0,
                'risk_grade': 'C',
                'sharpe_ratio': 0.0,
                'var_95': 0.0,
                'var_99': 0.0,
                'cvar_95': 0.0,
                'volatility': 0.0,
                'beta': 1.0,
                'sortino_ratio': 0.0
            }
        
        db.close()
        
        response = {
            'portfolio_metrics': portfolio_metrics,
            'position_metrics': position_metrics,
            'warnings': all_warnings
        }
        
        # Sanitize NaN/inf values before returning
        return sanitize_for_json(response)
        
    except Exception as e:
        print(f"Risk assessment error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
