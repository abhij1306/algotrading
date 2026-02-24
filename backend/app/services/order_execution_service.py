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
from ..utils.helpers import safe_float

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
        self._mode = "PAPER"  # Default mode for legacy callers

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

    def _estimate_market_reference_price(
        self, fyers_symbol: str, fallback_price: float = 0.0
    ) -> float:
        try:
            quote = self.broker.get_quote(fyers_symbol)
            value = quote.get("v", {}) if isinstance(quote, dict) else {}
            lp = safe_float(value.get("lp"), 0.0)
            if lp > 0:
                return lp
            return safe_float(quote.get("last_price"), fallback_price)
        except Exception:
            return fallback_price

    def _simulate_paper_fill(
        self, order_params: dict[str, Any], fyers_symbol: str
    ) -> dict[str, Any]:
        side = str(order_params.get("side", "BUY")).upper()
        order_type = str(order_params.get("type", "MARKET")).upper()
        qty = max(0, int(order_params.get("quantity", 0)))
        price = self._safe_float(order_params.get("price"), 0.0)
        trigger_price = self._safe_float(order_params.get("trigger_price"), 0.0)
        ref = self._estimate_market_reference_price(fyers_symbol, fallback_price=price)
        if ref <= 0:
            ref = price

        slippage_bps = self._safe_float(order_params.get("paper_slippage_bps", 5.0), 5.0)
        fee_bps = self._safe_float(order_params.get("paper_fee_bps", 3.5), 3.5)

        status = "SUBMITTED"
        fill_price = 0.0
        filled_qty = 0
        reason = "Pending in paper book"

        def market_fill(px: float) -> float:
            slip = px * (slippage_bps / 10000.0)
            return px + slip if side == "BUY" else max(0.0, px - slip)

        if order_type == "MARKET":
            fill_price = market_fill(ref)
            status = "FILLED"
            filled_qty = qty
            reason = "Paper simulated market fill"
        elif order_type == "LIMIT":
            if (side == "BUY" and price >= ref) or (side == "SELL" and price <= ref):
                fill_price = market_fill(min(price, ref) if side == "BUY" else max(price, ref))
                status = "FILLED"
                filled_qty = qty
                reason = "Paper simulated marketable limit fill"
            else:
                reason = "Paper limit resting (not marketable)"
        elif order_type == "SL":
            triggered = (side == "BUY" and trigger_price <= ref) or (
                side == "SELL" and trigger_price >= ref
            )
            if (
                triggered
                and price > 0
                and ((side == "BUY" and price >= ref) or (side == "SELL" and price <= ref))
            ):
                fill_price = market_fill(min(price, ref) if side == "BUY" else max(price, ref))
                status = "FILLED"
                filled_qty = qty
                reason = "Paper simulated stop-limit fill"
            else:
                reason = "Paper stop-limit pending trigger/fill"
        elif order_type == "SL-M":
            triggered = (side == "BUY" and trigger_price <= ref) or (
                side == "SELL" and trigger_price >= ref
            )
            if triggered:
                fill_price = market_fill(ref)
                status = "FILLED"
                filled_qty = qty
                reason = "Paper simulated stop-market fill"
            else:
                reason = "Paper stop-market pending trigger"
        else:
            reason = f"Paper unsupported order type {order_type}"

        notional = fill_price * filled_qty
        estimated_charges = round(notional * (fee_bps / 10000.0), 2) if filled_qty > 0 else 0.0
        fill_price = round(fill_price, 2) if fill_price > 0 else 0.0

        return {
            "status": status,
            "fill_price": fill_price,
            "filled_qty": filled_qty,
            "estimated_charges": estimated_charges,
            "slippage_bps": slippage_bps,
            "fill_source": "paper_sim",
            "message": reason,
        }

    def place_order(
        self, order_params: dict[str, Any], db: Session, mode: str | None = None
    ) -> dict[str, Any]:
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
            effective_mode = (mode or self._mode).upper()
            if effective_mode not in {"PAPER", "LIVE"}:
                raise ValueError("Invalid mode. Use PAPER or LIVE")

            symbol = order_params.get("symbol")
            side = order_params.get("side", "BUY")
            quantity = int(order_params.get("quantity", 0))
            if quantity <= 0:
                raise ValueError("Quantity must be positive")

            # 1. Live confirmation + risk checks
            if effective_mode == "LIVE":
                if not bool(order_params.get("is_live_confirmation_ack", False)):
                    return {
                        "status": "ERROR",
                        "message": "LIVE order requires explicit confirmation acknowledgment",
                        "code": "LIVE_CONFIRMATION_REQUIRED",
                    }

                risk_result = risk_manager.pre_trade_check(order_params, db)
                if risk_result.status.value == "FAIL":
                    return {
                        "status": "ERROR",
                        "message": f"Risk Check Failed: {risk_result.message}",
                        "code": risk_result.code,
                    }
                # Warn but allow if WARNING
                if risk_result.status.value == "WARNING":
                    if not str(order_params.get("risk_override_reason", "")).strip():
                        return {
                            "status": "ERROR",
                            "message": f"Risk warning requires override reason: {risk_result.message}",
                            "code": "RISK_WARNING_OVERRIDE_REQUIRED",
                        }
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

            is_paper_order = 1 if effective_mode == "PAPER" else 0

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
                is_paper=is_paper_order,
            )
            db.add(new_order)
            db.flush()  # Flush to get constraints checked, but don't commit yet
            # 3. Route Order
            response = {}
            if effective_mode == "LIVE":
                # Prepare Broker Payload
                broker_order = {
                    "symbol": fyers_symbol,
                    "quantity": quantity,
                    "side": side,
                    "type": order_params.get("type", "MARKET"),
                    "product": order_params.get("product", "INTRADAY"),
                    "price": float(order_params.get("price", 0.0)),
                }

                # Call Broker
                try:
                    broker_resp = self.broker.place_order(broker_order)

                    if not isinstance(broker_resp, dict):
                        logger.error("Broker returned non-dict response: %s", broker_resp)
                        new_order.status = "ERROR"
                        new_order.reject_reason = "Malformed broker response"
                        response = {"status": "ERROR", "message": "Malformed broker response"}
                    elif broker_resp.get("status") == "SUBMITTED" and broker_resp.get("order_id"):
                        new_order.id = str(broker_resp.get("order_id"))  # Update with Broker ID
                        new_order.status = "SUBMITTED"
                        new_order.broker_message = str(broker_resp.get("message", "Submitted"))
                        response = broker_resp
                    else:
                        new_order.status = "REJECTED"
                        new_order.reject_reason = str(
                            broker_resp.get("message", "Order rejected by broker")
                        )
                        response = broker_resp
                except Exception as e:
                    new_order.status = "ERROR"
                    new_order.reject_reason = str(e)
                    response = {"status": "ERROR", "message": str(e)}

            else:
                # PAPER MODE
                paper_fill = self._simulate_paper_fill(order_params, fyers_symbol)
                new_order.status = paper_fill["status"]
                new_order.filled_qty = int(paper_fill["filled_qty"])
                new_order.average_price = float(paper_fill["fill_price"])
                new_order.broker_message = (
                    f"{paper_fill['message']} | fill_source={paper_fill['fill_source']} "
                    f"| slippage_bps={paper_fill['slippage_bps']} "
                    f"| estimated_charges={paper_fill['estimated_charges']}"
                )
                response = {
                    "order_id": internal_id,
                    "status": paper_fill["status"],
                    "message": paper_fill["message"],
                    "fill_source": paper_fill["fill_source"],
                    "slippage_bps": paper_fill["slippage_bps"],
                    "estimated_charges": paper_fill["estimated_charges"],
                    "average_price": paper_fill["fill_price"],
                    "filled_qty": paper_fill["filled_qty"],
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
                return {
                    "status": "ERROR",
                    "message": "Cannot modify Paper order in Live mode (if originated as paper)",
                }

            try:
                resp = self.broker.modify_order(order_id, params)
                # Fyers validation needed here
                # Assuming resp is consistent dict
                status = resp.get("s", "error")
                msg = resp.get("message", "Modify failed")

                if status == "ok":
                    if "quantity" in params:
                        order.quantity = params["quantity"]
                    if "price" in params:
                        order.price = params["price"]
                    order.broker_message = f"Modified: {msg}"
                    db.commit()
                    return {"status": "SUCCESS", "message": "Order Modified"}
                else:
                    return {"status": "ERROR", "message": msg}
            except Exception as e:
                return {"status": "ERROR", "message": str(e)}
        else:
            # Paper Mode Modify
            if "quantity" in params:
                order.quantity = params["quantity"]
            if "price" in params:
                order.price = params["price"]
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
                resp = self.broker.cancel_order(order.id)  # Use broker ID
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
        return (
            db.query(LiveOrder)
            .filter(func.date(LiveOrder.created_at) == today)
            .order_by(LiveOrder.created_at.desc())
            .limit(limit)
            .all()
        )


# Singleton
order_execution_service = OrderExecutionService()
