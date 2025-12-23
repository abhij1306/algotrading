from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Dict, Any, Optional
from pydantic import BaseModel
from datetime import datetime
import pandas as pd
from ..database import get_db, SessionLocal
from ..data_repository import DataRepository
from ..strategies import ORBStrategy
from ..strategies.backtest_engine import BacktestEngine, BacktestConfig

router = APIRouter()

class BacktestRequest(BaseModel):
    """Request model for backtesting"""
    strategy_name: str
    symbol: str
    start_date: str  # YYYY-MM-DD
    end_date: str  # YYYY-MM-DD
    timeframe: str = "5min"
    initial_capital: float = 100000
    params: Optional[Dict[str, Any]] = {}

@router.get("/list")
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

@router.get("/{strategy_name}/params")
async def get_strategy_params(strategy_name: str):
    """Get default parameters for a strategy"""
    params_map = {
        "ORB": {
            "opening_range_minutes": {"type": "number", "default": 5},
            "stop_loss_pct": {"type": "number", "default": 0.5},
            "take_profit_pct": {"type": "number", "default": 1.5},
            "max_positions_per_day": {"type": "number", "default": 1},
            "trade_type": {"type": "select", "default": "options", "options": ["options", "equity"]}
        }
    }
    
    if strategy_name not in params_map:
        raise HTTPException(status_code=404, detail="Strategy not found")
    
    return {"params": params_map[strategy_name]}

@router.post("/run")
async def run_backtest(request: BacktestRequest, db: Session = Depends(get_db)):
    """Run backtest for a strategy."""
    if not request.symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")
        
    repo = DataRepository(db)
    
    try:
        # Validate strategy
        if request.strategy_name != 'ORB':
            raise HTTPException(status_code=400, detail=f"Strategy {request.strategy_name} not found")
        
        # Parse dates
        start_date = datetime.strptime(request.start_date, "%Y-%m-%d")
        end_date = datetime.strptime(request.end_date, "%Y-%m-%d")
        
        # Fetch data
        if request.timeframe in ['1min', '5min', '15min', '30min', '60min']:
            timeframe_map = {'1min': 1, '5min': 5, '15min': 15, '30min': 30, '60min': 60}
            tf_minutes = timeframe_map.get(request.timeframe, 5)
            
            hist = repo.get_intraday_candles(
                symbol=request.symbol,
                timeframe=tf_minutes,
                start_date=start_date,
                end_date=end_date
            )
            
            if hist is None or hist.empty:
                # Fallback to daily
                days = (end_date - start_date).days + 30
                hist = repo.get_historical_prices(request.symbol, days=days)
        else:
            days = (end_date - start_date).days + 30
            hist = repo.get_historical_prices(request.symbol, days=days)

        if hist is None or hist.empty:
             raise HTTPException(status_code=400, detail=f"No data available for {request.symbol}")

        # Normalize data (ensure lower case cols)
        hist.columns = hist.columns.str.lower()
        if 'timestamp' not in hist.columns:
            hist = hist.reset_index()
            if 'date' in hist.columns:
                hist['timestamp'] = pd.to_datetime(hist['date'])
            elif 'Date' in hist.columns:
                hist['timestamp'] = pd.to_datetime(hist['Date'])
            else:
                 hist['timestamp'] = pd.to_datetime(hist.iloc[:, 0])
        
        # Filter to date range
        hist = hist[(hist['timestamp'] >= start_date) & (hist['timestamp'] <= end_date)].copy()
        
        if len(hist) == 0:
             raise HTTPException(status_code=400, detail="No data in date range")

        # Initialize strategy
        strategy_params = {
            'symbol': request.symbol,
            'opening_range_minutes': request.params.get('opening_range_minutes', 5),
            'stop_loss_atr_multiplier': request.params.get('stopLoss', 0.5),
            'take_profit_atr_multiplier': request.params.get('takeProfit', 1.5),
            'max_positions_per_day': request.params.get('max_positions_per_day', 1),
            'trade_type': request.params.get('trade_type', 'options')
        }
        strategy = ORBStrategy(strategy_params)
        
        # Initialize config
        config = BacktestConfig(
            initial_capital=request.initial_capital,
            commission_pct=0.03,
            slippage_pct=0.05,
            max_positions=1,
            risk_per_trade_pct=request.params.get('riskPerTrade', 2.0)
        )
        
        print(f"[BACKTEST] Running {request.strategy_name} on {request.symbol}")
        print(f"[BACKTEST] Data Length: {len(hist)} rows")
        if not hist.empty:
            print(f"[BACKTEST] Data Range: {hist['timestamp'].min()} to {hist['timestamp'].max()}")
            print(f"[BACKTEST] Sample Row: {hist.iloc[0].to_dict()}")
        
        engine = BacktestEngine(strategy, config)
        results = engine.run(hist, request.symbol)
        
        print(f"[BACKTEST] Completed. Trades: {results['metrics']['total_trades']}, Equity: {results['metrics']['final_equity']}")
        
        return {
            "status": "success",
            "strategy": request.strategy_name,
            "symbol": request.symbol,
            "period": {"start": request.start_date, "end": request.end_date},
            **results
        }
        
    except HTTPException:
        raise
    except Exception as e:
        # Return detailed error for debugging
        import traceback
        tb = traceback.format_exc()
        print(tb)
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=500, content={"detail": str(e), "traceback": tb})
