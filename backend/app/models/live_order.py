from sqlalchemy import Column, Date, DateTime, Float, Integer, String, Text, func

from ..base import Base


class LiveOrder(Base):
    __tablename__ = "live_orders"

    id = Column(String(50), primary_key=True)  # Broker order ID
    internal_id = Column(String(50), unique=True, index=True)  # Our UUID
    user_id = Column(String(50), default="default_user")

    # Symbol Details
    symbol = Column(String(50), nullable=False)  # DB format: SBIN, NIFTY22500CE
    fyers_symbol = Column(String(80))  # NSE:SBIN-EQ, NSE:NIFTY2520822500CE
    exchange = Column(String(10), default="NSE")

    # Order Details
    side = Column(String(10), nullable=False)  # BUY / SELL
    quantity = Column(Integer, nullable=False)
    order_type = Column(String(20), nullable=False)  # MARKET / LIMIT / SL / SL-M
    product_type = Column(String(20), nullable=False)  # INTRADAY / MARGIN / CNC
    price = Column(Float, default=0.0)  # Limit price
    trigger_price = Column(Float, default=0.0)  # Stop loss trigger

    # Status
    status = Column(
        String(20), default="PENDING", index=True
    )  # PENDING -> SUBMITTED -> FILLED / REJECTED / CANCELLED
    filled_qty = Column(Integer, default=0)
    average_price = Column(Float, default=0.0)
    reject_reason = Column(Text, nullable=True)
    broker_message = Column(Text, nullable=True)

    # Options Specific Fields
    instrument_type = Column(String(10))  # EQ / FUT / CE / PE
    strike_price = Column(Float, nullable=True)
    expiry_date = Column(Date, nullable=True)
    underlying = Column(String(20), nullable=True)  # NIFTY, BANKNIFTY, RELIANCE
    option_type = Column(String(5), nullable=True)  # CE / PE

    # Meta / Tracking
    order_tag = Column(String(50), nullable=True)  # Strategy tag
    source = Column(String(20), default="MANUAL")  # MANUAL / AGENT / STRATEGY
    is_paper = Column(
        Integer, default=0
    )  # 1 = Paper, 0 = Live (using Integer for boolean compatibility if needed, else Boolean)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<LiveOrder {self.id} {self.side} {self.quantity} {self.symbol} @ {self.status}>"
