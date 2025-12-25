import datetime
import uuid
from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session
from ...database import SessionLocal, PaperOrder, PaperTrade, PaperPosition, PaperFund, PaperOrder as OrderModel
from ..base import IBroker, OrderResponse, Position, BrokerFunds
from ...utils.audit_logger import AuditLogger

class PaperBroker(IBroker):
    """
    Paper Trading Implementation of IBroker.
    Persists state to database (PaperLedger).
    Strictly follows IBroker interface.
    """
    
    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        paper_config = self.config.get('paper_trading', {})
        self.slippage_pct = paper_config.get('slippage_pct', 0.05)
        self.commission_per_trade = paper_config.get('commission_per_trade', 20)
        self.user_id = 'default_user'
        
        # Ensure funds exist
        self._init_funds()

    def _get_db(self):
        return SessionLocal()

    def _init_funds(self):
        db = self._get_db()
        try:
            fund = db.query(PaperFund).filter_by(user_id=self.user_id).first()
            if not fund:
                fund = PaperFund(user_id=self.user_id, available_balance=1000000.0, total_balance=1000000.0)
                db.add(fund)
                db.commit()
        finally:
            db.close()



    def place_order(self, order: Dict[str, Any]) -> OrderResponse:
        """
        Place order and persist to DB.
        Simulates instant fill for MARKET orders.
        """
        start_ts = datetime.datetime.now()
        db = self._get_db()
        try:
            # 1. Create Order Record
            order_id = str(uuid.uuid4())
            symbol = order.get('symbol')
            quantity = order.get('quantity')
            side = order.get('side', 'BUY') # Ex: BUY/SELL
            order_type = order.get('type', 'MARKET')
            price = order.get('price', 0)
            
            db_order = OrderModel(
                id=order_id,
                user_id=self.user_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type=order_type,
                price=price,
                status='PENDING'
            )
            db.add(db_order)
            
            # 2. Simulate Fill (Instant for Paper)
            fill_price = price
            # Get latest price if 0 or MARKET (mocking for now, ideally fetch from Data Repo)
            if fill_price == 0: 
                 fill_price = 100.0 # Fallback if no price provided in paper mode
            
            # Apply Slippage
            if self.slippage_pct > 0:
                 slippage = fill_price * (self.slippage_pct / 100)
                 if side == 'BUY': fill_price += slippage
                 else: fill_price -= slippage
            
            # 3. Create Trade Record
            db_trade = PaperTrade(
                id=str(uuid.uuid4()),
                order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=fill_price,
                value=fill_price * quantity,
                commission=self.commission_per_trade
            )
            db.add(db_trade)
            
            # 4. Update Order Status
            db_order.status = 'FILLED'
            db_order.average_price = fill_price
            db_order.filled_quantity = quantity
            
            # 5. Update Position (Netting)
            self._update_position(db, symbol, side, quantity, fill_price)
            
            # 6. Update Funds
            self._update_funds(db, side, fill_price * quantity, self.commission_per_trade)
            
            db.commit()
            
            resp = OrderResponse(
                order_id=order_id,
                status="FILLED",
                message="Paper Order Filled & Persisted",
                details={"average_price": round(fill_price, 2)}
            )
            
            # Log Success
            duration = int((datetime.datetime.now() - start_ts).total_seconds() * 1000)
            AuditLogger.log_api_call(
                broker_name="PaperBroker",
                method="place_order",
                params=order,
                response=resp,
                duration_ms=duration
            )
            return resp
            
        except Exception as e:
            db.rollback()
            print(f"Error placing paper order: {e}")
            
            # Log Failure
            duration = int((datetime.datetime.now() - start_ts).total_seconds() * 1000)
            AuditLogger.log_api_call(
                broker_name="PaperBroker",
                method="place_order",
                params=order,
                response={},
                duration_ms=duration,
                error=str(e)
            )
            
            return OrderResponse(
                order_id="",
                status="REJECTED",
                message=str(e),
                details=None
            )
        finally:
            db.close()

    def _update_position(self, db: Session, symbol: str, side: str, qty: int, price: float):
        """Update position table with netting logic"""
        # simplified netting: 
        # If BUY -> Add to quantity if LONG, Subtract if SHORT
        # If SELL -> Add to quantity if SHORT, Subtract if LONG
        
        # Check existing position
        pos = db.query(PaperPosition).filter_by(user_id=self.user_id, symbol=symbol).first()
        
        if not pos:
            # New Position
            new_pos = PaperPosition(
                user_id=self.user_id,
                symbol=symbol,
                side='LONG' if side == 'BUY' else 'SHORT',
                quantity=qty,
                average_price=price,
                ltp=price
            )
            db.add(new_pos)
        else:
            # Update Existing
            curr_side = pos.side
            
            if (side == 'BUY' and curr_side == 'LONG') or (side == 'SELL' and curr_side == 'SHORT'):
                # Adding to position
                total_val = (pos.quantity * pos.average_price) + (qty * price)
                pos.quantity += qty
                pos.average_price = total_val / pos.quantity
            else:
                # Reducing/Closing/Flipping position
                if pos.quantity > qty:
                    # Partial reduction
                    pos.quantity -= qty
                    # Avg price doesn't change on reduction (FIFO)
                elif pos.quantity == qty:
                    # Closed
                    db.delete(pos)
                else:
                    # Flip (e.g. Long 10, Sell 20 -> Short 10)
                    remaining = qty - pos.quantity
                    pos.side = 'SHORT' if side == 'SELL' else 'LONG'
                    pos.quantity = remaining
                    pos.average_price = price

    def _update_funds(self, db: Session, side: str, value: float, commission: float):
        fund = db.query(PaperFund).filter_by(user_id=self.user_id).first()
        if fund:
            fund.total_balance -= commission
            # Note: For paper trading, we simplify P&L realization. 
            # Ideally realized P&L updates total_balance. 
            # Here we just treat 'value' updates on close. 
            # Keeping it simple: Commission deduction is the only immediate balance impact.

    def get_positions(self) -> List[Position]:
        db = self._get_db()
        try:
            positions = db.query(PaperPosition).filter_by(user_id=self.user_id).all()
            return [Position(
                symbol=p.symbol,
                side=p.side,
                quantity=p.quantity,
                entry_price=p.average_price,
                current_price=p.ltp or p.average_price,
                pnl=( (p.ltp or p.average_price) - p.average_price) * p.quantity if p.side == 'LONG' else (p.average_price - (p.ltp or p.average_price)) * p.quantity,
                product_type=p.product_type
            ) for p in positions]
        finally:
            db.close()

    def get_orders(self) -> List[Dict[str, Any]]:
        db = self._get_db()
        try:
            orders = db.query(OrderModel).filter_by(user_id=self.user_id).order_by(OrderModel.created_at.desc()).all()
            return [{
                'id': o.id,
                'symbol': o.symbol,
                'side': o.side,
                'quantity': o.quantity,
                'price': o.price,
                'status': o.status,
                'created_at': o.created_at.isoformat()
            } for o in orders]
        finally:
            db.close()

    def modify_order(self, order_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "REJECTED", "message": "Modification not supported in simple paper mode"}

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        return {"status": "REJECTED", "message": "Order already filled (simulated)"}

    def get_holdings(self) -> List[Dict[str, Any]]:
        # Paper trading doesn't simulate Demat holdings distinct from positions yet
        return []

    def get_funds(self) -> BrokerFunds:
        db = self._get_db()
        try:
            fund = db.query(PaperFund).filter_by(user_id=self.user_id).first()
            if fund:
                 return BrokerFunds(available=fund.available_balance, used=fund.used_margin, total=fund.total_balance)
            else:
                 return BrokerFunds(available=0, used=0, total=0)
        finally:
            db.close()
            
    def get_quote(self, symbol: str) -> Dict[str, Any]:
        return {"ltp": 0}

    def update_pnl(self, current_prices: Dict[str, float]):
        """Update LTP and PnL in DB for all positions"""
        db = self._get_db()
        try:
            positions = db.query(PaperPosition).filter_by(user_id=self.user_id).all()
            for pos in positions:
                current_price = current_prices.get(pos.symbol)
                if current_price:
                    pos.ltp = current_price
                    if pos.side == 'LONG':
                        pos.pnl = (current_price - pos.average_price) * pos.quantity
                    else:
                        pos.pnl = (pos.average_price - current_price) * pos.quantity
            db.commit()
        finally:
            db.close()
