"""
Core data models for Smart Trader system
"""
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any
from datetime import datetime
from enum import Enum


class Direction(str, Enum):
    """Trade direction"""
    LONG = "LONG"
    SHORT = "SHORT"


class ConfidenceLevel(str, Enum):
    """Signal confidence levels"""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(frozen=True)
class MarketSnapshot:
    """
    Immutable snapshot of market data for a symbol at a point in time.
    Handles missing fields gracefully with Optional types.
    """
    symbol: str
    timestamp: datetime
    timeframe: str  # "5m", "15m", "1h", "1d"
    
    # Price data
    open: float
    high: float
    low: float
    close: float
    volume: int
    
    # Technical indicators (all optional)
    ema_9: Optional[float] = None
    ema_21: Optional[float] = None
    ema_50: Optional[float] = None
    rsi: Optional[float] = None
    atr: Optional[float] = None
    
    # Volume metrics
    avg_volume_20: Optional[float] = None
    volume_ratio: Optional[float] = None  # current_volume / avg_volume
    
    # Trend metrics
    trend_strength: Optional[float] = None  # 0-1 scale
    volatility: Optional[float] = None
    
    # Index context
    nifty_change_pct: Optional[float] = None
    correlation_with_nifty: Optional[float] = None
    relative_strength: Optional[float] = None  # vs Nifty
    
    # Recent behavior
    prev_close: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    day_change_pct: Optional[float] = None
    
    # Additional context
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def change_pct(self) -> float:
        """Calculate percentage change from open to close"""
        if self.open == 0:
            return 0.0
        return ((self.close - self.open) / self.open) * 100
    
    @property
    def range_pct(self) -> float:
        """Calculate day range as percentage of close"""
        if self.close == 0:
            return 0.0
        return ((self.high - self.low) / self.close) * 100
    
    def has_indicator(self, indicator: str) -> bool:
        """Check if indicator is available"""
        return getattr(self, indicator, None) is not None
