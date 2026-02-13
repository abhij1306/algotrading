from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Index, JSON, Text
from datetime import datetime
from ..base import Base

class PaperOrder(Base):
    """Paper Trading Order Book"""
    __tablename__ = "paper_orders"

    id = Column(String(50), primary_key=True)
    user_id = Column(String(50), default='default_user', index=True)
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False)
    quantity = Column(Integer, nullable=False)
    product_type = Column(String(20), default='INTRADAY')
    order_type = Column(String(20), default='MARKET')

    price = Column(Float)
    trigger_price = Column(Float)

    status = Column(String(20), default='PENDING', index=True)
    filled_quantity = Column(Integer, default=0)
    average_price = Column(Float)

    message = Column(Text)
    parent_order_id = Column(String(50))

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PaperTrade(Base):
    """Paper Trading Trade Book (Fills)"""
    __tablename__ = "paper_trades"

    id = Column(String(50), primary_key=True)
    order_id = Column(String(50), ForeignKey("paper_orders.id"), nullable=False)
    symbol = Column(String(20), nullable=False)
    side = Column(String(10), nullable=False)
    quantity = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    value = Column(Float, nullable=False)

    commission = Column(Float, default=0.0)
    realized_pnl = Column(Float)

    trade_time = Column(DateTime, default=datetime.utcnow)


class PaperPosition(Base):
    """Paper Trading Open Positions"""
    __tablename__ = "paper_positions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(50), default='default_user')
    symbol = Column(String(20), nullable=False, index=True)
    product_type = Column(String(20), default='INTRADAY')
    side = Column(String(10), nullable=False)

    quantity = Column(Integer, nullable=False)
    average_price = Column(Float, nullable=False)

    ltp = Column(Float)
    pnl = Column(Float)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index('ix_paper_position_sym', 'user_id', 'symbol', 'product_type', unique=True),
    )


class PaperFund(Base):
    """Paper Trading Funds (Ledger)"""
    __tablename__ = "paper_funds"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(50), unique=True, nullable=False)

    available_balance = Column(Float, default=1000000.0)
    used_margin = Column(Float, default=0.0)
    total_balance = Column(Float, default=1000000.0)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class ActionCenter(Base):
    """Pending Actions/Orders requiring human approval"""
    __tablename__ = "action_center"

    id = Column(Integer, primary_key=True, index=True)
    source_agent = Column(String(50), nullable=False)
    action_type = Column(String(50), nullable=False)

    # Action Details (JSON)
    payload = Column(JSON, nullable=False)

    # Context
    reason = Column(String(500))
    confidence = Column(Float)

    # Status
    status = Column(String(20), default='PENDING', index=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
