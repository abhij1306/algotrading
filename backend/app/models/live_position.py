from sqlalchemy import Column, Date, DateTime, Float, Integer, String, func

from ..base import Base


class LivePosition(Base):
    """
    Represents a real-time position synced from the broker.
    This creates a snapshot of the current holdings.
    """
    __tablename__ = "live_positions"

    id = Column(String(100), primary_key=True)        # Composite ID: UID-SYMBOL-PRODUCT
    user_id = Column(String(50), default='default_user')

    symbol = Column(String(50), nullable=False, index=True)
    fyers_symbol = Column(String(80))

    product_type = Column(String(20))                 # INTRADAY / MARGIN / CNC
    side = Column(String(10))                         # LONG / SHORT

    # Quantities
    net_qty = Column(Integer, default=0)
    buy_qty = Column(Integer, default=0)
    sell_qty = Column(Integer, default=0)

    # Prices
    buy_avg = Column(Float, default=0.0)
    sell_avg = Column(Float, default=0.0)
    net_avg = Column(Float, default=0.0)
    ltp = Column(Float, default=0.0)

    # P&L (Broker calculated or locally estimated)
    realized_pl = Column(Float, default=0.0)
    unrealized_pl = Column(Float, default=0.0)
    pl_total = Column(Float, default=0.0)

    # Options Details (if applicable)
    instrument_type = Column(String(10))              # EQ / FUT / CE / PE
    strike_price = Column(Float, nullable=True)
    expiry_date = Column(Date, nullable=True)

    last_synced_at = Column(DateTime, default=func.now())

    def __repr__(self):
        return f"<LivePosition {self.symbol} {self.net_qty} @ {self.net_avg}>"
