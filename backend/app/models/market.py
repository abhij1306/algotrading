from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, Index, Integer, String, Text, func

from ..base import Base


class MarketNews(Base):
    """Market news articles for real-time news ticker"""

    __tablename__ = "market_news"

    id = Column(Integer, primary_key=True, index=True)

    # Article details
    title = Column(String(500), nullable=False)
    summary = Column(Text)
    source = Column(String(100))
    url = Column(Text)

    # Timing
    published_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Stock symbols mentioned
    symbols = Column(String(500))

    # Sentiment analysis
    sentiment = Column(String(20))

    # Indexes
    __table_args__ = (
        Index("ix_published_at", "published_at", postgresql_ops={"published_at": "DESC"}),
        Index("ix_symbols", "symbols"),
    )


class Watchlist(Base):
    __tablename__ = "watchlist"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, unique=True, index=True)
    instrument_type = Column(String, default="EQ")
    added_at = Column(DateTime, default=func.now())


class StockUniverse(Base):
    """Immutable stock universe definitions with historical membership"""

    __tablename__ = "stock_universes"

    id = Column(String(50), primary_key=True)
    description = Column(String(500))
    symbols_by_date = Column(JSON, nullable=False)
    rebalance_frequency = Column(String(20))
    selection_rules = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
