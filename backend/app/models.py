"""
Pydantic models for API requests and responses
"""
from pydantic import BaseModel
from typing import List, Optional

class StockFeatures(BaseModel):
    symbol: str
    close: float
    ema20: float
    ema34: float
    ema50: float
    atr: float
    rsi: float
    atr_pct: float
    adv20: int
    vol_percentile: float
    z_close: float
    z_atr_pct: float
    price_above_ema50: bool
    ema20_above_50: bool
    is_20d_breakout: bool
    intraday_score: Optional[float] = None
    swing_score: Optional[float] = None

class ScreenerStats(BaseModel):
    total_screened: int
    features_computed: int
    intraday_count: int
    swing_count: int
    combined_count: int

class ScreenerResponse(BaseModel):
    intraday: List[dict]
    swing: List[dict]
    combined: List[dict]
    stats: ScreenerStats
    generated_at: str
    mode: str

class HealthResponse(BaseModel):
    status: str
    mode: str
    has_fyers: bool
