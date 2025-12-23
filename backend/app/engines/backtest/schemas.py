"""
Pydantic schemas for backtest configuration
"""
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional
from datetime import datetime


class StrategyParams(BaseModel):
    """Dynamic strategy parameters (for Analyst mode)"""
    stopLoss: float = Field(default=1.5, description="ATR multiplier for stop loss")
    takeProfit: float = Field(default=3.0, description="ATR multiplier for take profit")
    riskPerTrade: float = Field(default=1.0, description="Risk per trade as % of capital")
    # Add more as needed
    
    class Config:
        extra = "allow"  # Allow additional params


class BacktestConfig(BaseModel):
    """Backtest execution configuration"""
    initial_capital: float = Field(default=100000, description="Starting capital")
    commission_pct: float = Field(default=0.03, description="Commission as % of trade value")
    slippage_pct: float = Field(default=0.05, description="Slippage as % of price")
    max_positions: int = Field(default=1, description="Max concurrent positions")
    force_intraday_exit: bool = Field(default=False, description="Force EOD exit at 15:15")
    
    class Config:
        frozen = True  # Immutable


class BacktestRequest(BaseModel):
    """API request for backtesting"""
    strategy_name: str
    symbol: str
    start_date: str  # YYYY-MM-DD
    end_date: str
    timeframe: str = "5min"
    initial_capital: float = 100000
    params: Optional[Dict[str, Any]] = {}


class BacktestResult(BaseModel):
    """Backtest execution results"""
    strategy: str
    symbol: str
    start_date: str
    end_date: str
    
    # Metrics
    total_return_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    win_rate_pct: float
    total_trades: int
    winning_trades: int
    cagr_pct: float
    profit_factor: float
    final_equity: float
    
    # Time series
    daily_equity: list = [] # Alias for frontend compat
    equity_curve: list
    trades: list
    
    class Config:
        arbitrary_types_allowed = True
