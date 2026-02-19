import logging
import uuid
from datetime import date
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..brokers.plugins.fyers import FyersBroker
from ..models.live_order import LiveOrder
from ..services.risk_manager import risk_manager
from ..services.symbol_master import symbol_master

logger = logging.getLogger(__name__)

class OrderExecutionService:
    """
    Central service for handling order execution.
    - Routes orders to Broker (Live) or Simulator (Paper)
    - Persists order state to Database
    - Performs pre-trade validation
    """

    def __init__(self):
        self._broker = None
        self._mode = "PAPER" # Default to PAPER

    @property
    def broker(self):
        if not self._broker:
            self._broker = FyersBroker()
        return self._broker

    def set_mode(self, mode: str):
        """Set trading mode: 'PAPER' or 'LIVE'"""
        if mode.upper() not in ["PAPER", "LIVE"]:
            raise ValueError("Invalid mode. Use PAPER or LIVE")
        self._mode = mode.upper()
        logger.info(f"Trading mode set to {self._mode}")

    def get_mode(self) -> str:
        return self._mode

    def place_order(self, order_params: dict[str, Any], db: Session) -> dict[str, Any]:
        """
        Place an order.

        Args:
            order_params: {
                "symbol": "SBIN",
                "side": "BUY",
                "quantity": 1,
                "product": "INTRADAY",
                "type": "MARKET",
                "price": 0,
                "user_id": "default",
                "tag": "manual"
            }
            db: Database session

        Returns:
            Dict with order_id, status, message
        """
        try:
            symbol = order_params.get("symbol")
            side = order_params.get("side", "BUY")
            quantity = int(order_params.get("quantity", 0))
            if quantity <= 0:
                raise ValueError("Quantity must be positive")

            # 1. Risk Check (for LIVE mode)
            if self._mode == "LIVE":
                risk_result = risk_manager.pre_trade_check(order_params, db)
                if risk_result.status.value == "FAIL":
                    return {
                        "status": "ERROR",
                        "message": f"Risk Check Failed: {risk_result.message}",
                        "code": risk_result.code
                    }
                # Warn but allow if WARNING
                if risk_result.status.value == "WARNING":
                    logger.warning(f"Risk Warning: {risk_result.message}")

            # 2. Validate Symbol & Format
            db_symbol = symbol_master.to_db(symbol)

            # Determine Fyers Symbol
            fyers_symbol = order_params.get("fyers_symbol")
            if not fyers_symbol:
                try:
                    fyers_symbol = symbol_master.to_fyers(db_symbol)
                except Exception:
                    fyers_symbol = symbol or db_symbol

            # 2. Create DB Record (PENDING)
            internal_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"

            is_paper_order = (1 if self._mode == "PAPER" else 0)

            new_order = LiveOrder(
                id=internal_id,
                internal_id=internal_id,
                user_id=order_params.get("user_id", "default"),
                symbol=db_symbol,
                fyers_symbol=fyers_symbol,
                side=side,
                quantity=quantity,
                order_type=order_params.get("type", "MARKET"),
                product_type=order_params.get("product", "INTRADAY"),
                price=float(order_params.get("price", 0)),
                trigger_price=float(order_params.get("trigger_price", 0)),
                status="PENDING",
                instrument_type=order_params.get("instrument_type", "EQ"),
                strike_price=order_params.get("strike_price"),
                order_tag=order_params.get("tag"),
                source=order_params.get("source", "MANUAL"),
                is_paper=is_paper_order
            )
            db.add(new_order)
            db.flush()  # Flush to get constraints checked, but don't commit yet
            # 3. Route Order
            response = {}
            if self._mode == "LIVE":
                # Prepare Broker Payload
                broker_order = {
                    "symbol": fyers_symbol,
                    "quantity": quantity,
                    "side": side,
                    "type": order_params.get("type", "MARKET"),
                    "product": order_params.get("product", "INTRADAY"),
                    "price": float(order_params.get("price", 0.0))
                }

                # Call Broker
                try:
                    broker_resp = self.broker.place_order(broker_order)

                    if broker_resp["status"] == "SUBMITTED":
                        new_order.id = broker_resp["order_id"] # Update with Broker ID
                        new_order.status = "SUBMITTED"
                        new_order.broker_message = broker_resp["message"]
                        response = broker_resp
                    else:
                        new_order.status = "REJECTED"
                        new_order.reject_reason = broker_resp["message"]
                        response = broker_resp
                except Exception as e:
                    new_order.status = "ERROR"
                    new_order.reject_reason = str(e)
                    response = {"status": "ERROR", "message": str(e)}

            else:
                # PAPER MODE
                new_order.status = "SUBMITTED"
                new_order.broker_message = "Paper Order Placed"
                response = {
                    "order_id": internal_id,
                    "status": "SUBMITTED",
                    "message": "Paper Order Placed Successfully"
                }

            db.commit()
            return response

        except Exception as e:
            logger.error(f"Order Placement Failed: {e}")
            db.rollback()
            return {"status": "ERROR", "message": str(e)}
    def modify_order(self, order_id: str, params: dict[str, Any], db: Session) -> dict[str, Any]:
        """Modify an existing order"""
        order = db.query(LiveOrder).filter(LiveOrder.id == order_id).first()
        if not order:
            return {"status": "ERROR", "message": "Order not found"}

        if self._mode == "LIVE":
            if order.is_paper:
                return {"status": "ERROR", "message": "Cannot modify Paper order in Live mode (if originated as paper)"}

            try:
                resp = self.broker.modify_order(order_id, params)
                # Fyers validation needed here
                # Assuming resp is consistent dict
                status = resp.get("s", "error")
                msg = resp.get("message", "Modify failed")

                if status == "ok":
                    if "quantity" in params: order.quantity = params["quantity"]
                    if "price" in params: order.price = params["price"]
                    order.broker_message = f"Modified: {msg}"
                    db.commit()
                    return {"status": "SUCCESS", "message": "Order Modified"}
                else:
                    return {"status": "ERROR", "message": msg}
            except Exception as e:
                 return {"status": "ERROR", "message": str(e)}
        else:
            # Paper Mode Modify
            if "quantity" in params: order.quantity = params["quantity"]
            if "price" in params: order.price = params["price"]
            db.commit()
            return {"status": "SUCCESS", "message": "Paper Order Modified"}

    def cancel_order(self, order_id: str, db: Session) -> dict[str, Any]:
        """Cancel an order"""
        order = db.query(LiveOrder).filter(LiveOrder.id == order_id).first()
        if not order:
             # Try searching by internal_id if id match fails
            order = db.query(LiveOrder).filter(LiveOrder.internal_id == order_id).first()
            if not order:
                return {"status": "ERROR", "message": "Order not found"}

        if self._mode == "LIVE":
            if order.is_paper:
                 order.status = "CANCELLED"
                 db.commit()
                 return {"status": "SUCCESS", "message": "Paper Order Cancelled"}

            try:
                resp = self.broker.cancel_order(order.id) # Use broker ID
                status = resp.get("s", "error")
                msg = resp.get("message", "Cancel failed")

                if status == "ok":
                    order.status = "CANCELLED"
                    db.commit()
                    return {"status": "SUCCESS", "message": "Order Cancelled"}
                else:
                    # Check if already cancelled?
                    if "already cancelled" in str(msg).lower():
                        order.status = "CANCELLED"
                        db.commit()
                        return {"status": "SUCCESS", "message": "Order Already Cancelled"}
                    return {"status": "ERROR", "message": msg}
            except Exception as e:
                return {"status": "ERROR", "message": str(e)}
        else:
            # Paper Mode
            order.status = "CANCELLED"
            db.commit()
            return {"status": "SUCCESS", "message": "Paper Order Cancelled"}

    def get_todays_orders(self, db: Session, limit: int = 50) -> list[LiveOrder]:
        """Get list of today's orders"""
        today = date.today()
        # Ensure we filter by date correctly depending on DB dialect
        return db.query(LiveOrder).filter(func.date(LiveOrder.created_at) == today).order_by(LiveOrder.created_at.desc()).limit(limit).all()

# Singleton
order_execution_service = OrderExecutionService()
