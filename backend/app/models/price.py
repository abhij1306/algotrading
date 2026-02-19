from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import relationship

from ..base import Base


class HistoricalPrice(Base):
    """Daily OHLCV data with technical indicators"""
    __tablename__ = "historical_prices"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    date = Column(Date, nullable=False, index=True)

    # OHLCV
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(BigInteger, nullable=False)

    # Delivery data
    deliverable_qty = Column(BigInteger)
    delivery_pct = Column(Float)

    # Technical Indicators - Basic
    ema_20 = Column(Float)
    ema_50 = Column(Float)
    rsi_14 = Column(Float)
    atr_14 = Column(Float)

    # Technical Indicators - Advanced (NEW)
    macd = Column(Float)  # MACD line
    macd_signal = Column(Float)  # Signal line
    macd_histogram = Column(Float)  # Histogram
    stoch_k = Column(Float)  # Stochastic %K
    stoch_d = Column(Float)  # Stochastic %D
    bb_upper = Column(Float)  # Bollinger upper band
    bb_middle = Column(Float)  # Bollinger middle band (20 SMA)
    bb_lower = Column(Float)  # Bollinger lower band
    adx = Column(Float)  # Average Directional Index
    obv = Column(BigInteger)  # On-Balance Volume

    # Trend indicators
    high_20d = Column(Float)  # 20-day high
    is_breakout = Column(Boolean)  # Price at/above 20-day high

    # New Trend Metrics (Pre-calculated)
    trend_7d = Column(Float)   # % Change over 7 days (5 trading days)
    trend_30d = Column(Float)  # % Change over 30 days (21 trading days)

    # Data source tracking
    source = Column(String(20))  # 'fyers', 'yfinance', etc.
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))

    # Relationships
    company = relationship("Company", back_populates="historical_prices")

    # Composite index for fast queries
    __table_args__ = (
        Index('ix_company_date', 'company_id', 'date', unique=True),
    )

class IntradayCandle(Base):
    """Intraday OHLCV candle data for backtesting"""
    __tablename__ = "intraday_candles"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    timestamp = Column(DateTime, nullable=False, index=True)

    # Timeframe (1, 5, 15, 30, 60 minutes)
    timeframe = Column(Integer, nullable=False)  # in minutes

    # OHLCV
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(BigInteger, nullable=False)  # Use BigInteger to match HistoricalPrice and handle high-volume intraday data

    # Additional fields
    trades = Column(Integer)  # Number of trades in this candle

    # Data source tracking
    source = Column(String(20), default='fyers')  # 'fyers', 'zerodha', etc.
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))

    # Relationships
    company = relationship("Company")

    # Composite index for fast queries by company, timeframe, and timestamp
    __table_args__ = (
        Index('ix_intraday_company_tf_ts', 'company_id', 'timeframe', 'timestamp', unique=True),
        Index('ix_intraday_timestamp', 'timestamp'),
    )
