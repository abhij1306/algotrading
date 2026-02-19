from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, func

from ..base import Base


class LiveTrade(Base):
    __tablename__ = "live_trades"

    id = Column(String(50), primary_key=True)         # Broker trade ID
    order_id = Column(String(50), ForeignKey("live_orders.id"), nullable=False, index=True)
    internal_order_id = Column(String(50), index=True)

    symbol = Column(String(50), nullable=False)
    fyers_symbol = Column(String(80))

    side = Column(String(10), nullable=False)         # BUY / SELL
    filled_qty = Column(Integer, nullable=False)
    filled_price = Column(Float, nullable=False)
    trade_value = Column(Float, nullable=False)       # qty * price

    trade_time = Column(DateTime, nullable=False)     # Broker timestamp
    exchange_order_id = Column(String(50))

    created_at = Column(DateTime, default=func.now())

    def __repr__(self):
        return f"<LiveTrade {self.id} {self.side} {self.filled_qty} @ {self.filled_price}>"
