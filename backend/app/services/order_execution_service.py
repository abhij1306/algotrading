import logging
import os
import time
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

NO_PAPER_PRICE_MESSAGE = (
    "No market price available for {symbol}; paper {order_type} order rejected. "
    "Subscribe the symbol to live data or use a LIMIT order."
)
PAPER_TERMINAL_STATES = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
STOP_ORDER_TYPES = {"SL", "SL-M"}
LIMIT_PRICED_ORDER_TYPES = {"LIMIT", "SL"}
# Fyers tick fields carrying the exchange time, in epoch seconds, most precise first.
TICK_TIME_FIELDS = ("exch_feed_time", "last_traded_time")
DEFAULT_PAPER_TICK_MAX_AGE_SEC = 60.0


def order_price_error(order_type: str, price: float, trigger_price: float) -> str | None:
    """Message when an order's prices are invalid for its type, else None.

    Same rules and wording as FyersBroker.place_order / modify_order.
    """
    if order_type in STOP_ORDER_TYPES and trigger_price <= 0:
        return f"{order_type} order requires a positive trigger price."
    if order_type in LIMIT_PRICED_ORDER_TYPES and price <= 0:
        return f"{order_type} order requires a positive limit price."
    return None


def paper_tick_max_age_sec() -> float:
    max_age = safe_float(os.getenv("PAPER_TICK_MAX_AGE_SEC"), DEFAULT_PAPER_TICK_MAX_AGE_SEC)
    return max_age if max_age > 0 else DEFAULT_PAPER_TICK_MAX_AGE_SEC


def latest_tick_price(db_symbol: str) -> float:
    """Last traded price from the live market-data tick cache, or 0.0 if none.

    This reads the market-data feed, never the order broker, so PAPER fills can
    use it without reaching the live broker. The cache keeps ticks after a
    disconnect or market close, so a tick is used only while the market is open
    (DEV_MODE counts as open, as in LiveMarketService) and its exchange time is
    within PAPER_TICK_MAX_AGE_SEC (default 60s). A tick without a time is stale.
    """
    from .live_market_service import live_market

    tick = live_market.get_latest_tick(db_symbol)
    if not isinstance(tick, dict) or not live_market.is_market_open():
        return 0.0
    tick_time = next(
        (safe_float(tick.get(f), 0.0) for f in TICK_TIME_FIELDS if tick.get(f) is not None),
        0.0,
    )
    if tick_time <= 0 or time.time() - tick_time > paper_tick_max_age_sec():
        return 0.0
    price = safe_float(tick.get("ltp"), 0.0)
    return price if price > 0 else 0.0


class OrderExecutionService:
    """
    Central service for handling order execution.
    - Routes orders to Broker (Live) or Simulator (Paper)
    - Persists order state to Database
    - Performs pre-trade validation
    """

    def __init__(self) -> None:
        self._broker = None

    @property
    def broker(self) -> FyersBroker:
        if not self._broker:
            self._broker = FyersBroker()
        return self._broker

    @staticmethod
    def _paper_reference_price(db_symbol: str, limit_price: float) -> float:
        """Reference price for a PAPER fill: the latest market-data tick, else
        the order's own limit price, else 0.0 (caller rejects). Never the broker."""
        try:
            tick_price = latest_tick_price(db_symbol)
        except Exception:
            logger.exception("Paper reference price lookup failed for %s", db_symbol)
            tick_price = 0.0
        if tick_price > 0:
            return tick_price
        return limit_price if limit_price > 0 else 0.0

    def _simulate_paper_fill(self, order_params: dict[str, Any], db_symbol: str) -> dict[str, Any]:
        side = str(order_params.get("side", "BUY")).upper()
        order_type = str(order_params.get("type", "MARKET")).upper()
        qty = max(0, int(order_params.get("quantity", 0)))
        price = safe_float(order_params.get("price"), 0.0)
        trigger_price = safe_float(order_params.get("trigger_price"), 0.0)
        slippage_bps = safe_float(order_params.get("paper_slippage_bps", 5.0), 5.0)
        fee_bps = safe_float(order_params.get("paper_fee_bps", 3.5), 3.5)

        def rejected(message: str) -> dict[str, Any]:
            return {
                "status": "REJECTED",
                "fill_price": 0.0,
                "filled_qty": 0,
                "estimated_charges": 0.0,
                "slippage_bps": slippage_bps,
                "fill_source": "paper_sim",
                "message": message,
            }

        price_error = order_price_error(order_type, price, trigger_price)
        if price_error:
            return rejected(price_error)

        ref = self._paper_reference_price(db_symbol, price)
        if ref <= 0:
            return rejected(NO_PAPER_PRICE_MESSAGE.format(symbol=db_symbol, order_type=order_type))

        status = "SUBMITTED"
        fill_price = 0.0
        filled_qty = 0
        reason = "Pending in paper book"

        def market_fill(px: float) -> float:
            slip = px * (slippage_bps / 10000.0)
            return px + slip if side == "BUY" else max(0.0, px - slip)

        def limit_fill(px: float) -> float:
            # Slippage never fills a limit order beyond its limit price.
            filled = market_fill(px)
            return min(filled, price) if side == "BUY" else max(filled, price)

        if order_type == "MARKET":
            fill_price = market_fill(ref)
            status = "FILLED"
            filled_qty = qty
            reason = "Paper simulated market fill"
        elif order_type == "LIMIT":
            if (side == "BUY" and price >= ref) or (side == "SELL" and price <= ref):
                fill_price = limit_fill(ref)
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
                fill_price = limit_fill(ref)
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

    @staticmethod
    def _resolve_mode(order_params: dict[str, Any], mode: str | None) -> str:
        effective_mode = (mode or order_params.get("mode") or "PAPER").upper()
        if effective_mode not in {"PAPER", "LIVE"}:
            raise ValueError("Invalid mode. Use PAPER or LIVE")
        return effective_mode

    @staticmethod
    def _validate_quantity(order_params: dict[str, Any]) -> int:
        try:
            quantity = int(order_params.get("quantity", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid quantity") from exc
        if quantity <= 0:
            raise ValueError("Quantity must be positive")
        return quantity

    def _run_live_risk_checks(
        self, order_params: dict[str, Any], db: Session
    ) -> dict[str, Any] | None:
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
        if risk_result.status.value == "WARNING":
            if not str(order_params.get("risk_override_reason", "")).strip():
                return {
                    "status": "ERROR",
                    "message": f"Risk warning requires override reason: {risk_result.message}",
                    "code": "RISK_WARNING_OVERRIDE_REQUIRED",
                }
            logger.warning(f"Risk Warning: {risk_result.message}")
        return None

    def _resolve_symbols(self, order_params: dict[str, Any]) -> tuple[str, str]:
        symbol = order_params.get("symbol")
        db_symbol = symbol_master.to_db(symbol)
        fyers_symbol = order_params.get("fyers_symbol")
        if fyers_symbol:
            return db_symbol, fyers_symbol
        try:
            return db_symbol, symbol_master.to_fyers(db_symbol)
        except Exception:
            return db_symbol, symbol or db_symbol

    @staticmethod
    def _create_order_record(
        order_params: dict[str, Any],
        db_symbol: str,
        fyers_symbol: str,
        quantity: int,
        effective_mode: str,
    ) -> LiveOrder:
        internal_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
        return LiveOrder(
            id=internal_id,
            internal_id=internal_id,
            user_id=order_params.get("user_id", "default"),
            symbol=db_symbol,
            fyers_symbol=fyers_symbol,
            side=order_params.get("side", "BUY"),
            quantity=quantity,
            order_type=order_params.get("type", "MARKET"),
            product_type=order_params.get("product", "INTRADAY"),
            price=safe_float(order_params.get("price"), 0.0),
            trigger_price=safe_float(order_params.get("trigger_price"), 0.0),
            status="PENDING",
            instrument_type=order_params.get("instrument_type", "EQ"),
            strike_price=order_params.get("strike_price"),
            order_tag=order_params.get("tag"),
            source=order_params.get("source", "MANUAL"),
            is_paper=1 if effective_mode == "PAPER" else 0,
        )

    def _submit_live_order(
        self, order_params: dict[str, Any], fyers_symbol: str, new_order: LiveOrder
    ) -> dict[str, Any]:
        broker_order = {
            "symbol": fyers_symbol,
            "quantity": int(order_params.get("quantity", 0)),
            "side": order_params.get("side", "BUY"),
            "type": order_params.get("type", "MARKET"),
            "product": order_params.get("product", "INTRADAY"),
            "price": safe_float(order_params.get("price"), 0.0),
            "trigger_price": safe_float(order_params.get("trigger_price"), 0.0),
        }
        broker_resp = self.broker.place_order(broker_order)

        if not isinstance(broker_resp, dict):
            logger.error("Broker returned non-dict response: %s", broker_resp)
            new_order.status = "ERROR"
            new_order.reject_reason = "Malformed broker response"
            return {"status": "ERROR", "message": "Malformed broker response"}

        if broker_resp.get("status") == "SUBMITTED" and broker_resp.get("order_id"):
            new_order.id = str(broker_resp.get("order_id"))
            new_order.status = "SUBMITTED"
            new_order.broker_message = str(broker_resp.get("message", "Submitted"))
            return broker_resp

        new_order.status = "REJECTED"
        new_order.reject_reason = str(broker_resp.get("message", "Order rejected by broker"))
        return broker_resp

    @staticmethod
    def _apply_paper_fill(
        new_order: LiveOrder, internal_id: str, paper_fill: dict[str, Any]
    ) -> dict[str, Any]:
        new_order.status = paper_fill["status"]
        new_order.filled_qty = int(paper_fill["filled_qty"])
        new_order.average_price = float(paper_fill["fill_price"])
        if paper_fill["status"] == "REJECTED":
            new_order.reject_reason = paper_fill["message"]
        new_order.broker_message = (
            f"{paper_fill['message']} | fill_source={paper_fill['fill_source']} "
            f"| slippage_bps={paper_fill['slippage_bps']} "
            f"| estimated_charges={paper_fill['estimated_charges']}"
        )
        return {
            "order_id": internal_id,
            "status": paper_fill["status"],
            "message": paper_fill["message"],
            "fill_source": paper_fill["fill_source"],
            "slippage_bps": paper_fill["slippage_bps"],
            "estimated_charges": paper_fill["estimated_charges"],
            "average_price": paper_fill["fill_price"],
            "filled_qty": paper_fill["filled_qty"],
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
            effective_mode = self._resolve_mode(order_params, mode)
            quantity = self._validate_quantity(order_params)
            db_symbol, fyers_symbol = self._resolve_symbols(order_params)
            if effective_mode == "LIVE":
                # Risk price estimates quote the broker, which needs the Fyers symbol.
                risk_params = {**order_params, "fyers_symbol": fyers_symbol}
                risk_error = self._run_live_risk_checks(risk_params, db)
                if risk_error:
                    return risk_error

            new_order = self._create_order_record(
                order_params, db_symbol, fyers_symbol, quantity, effective_mode
            )
            db.add(new_order)
            db.flush()

            if effective_mode == "LIVE":
                try:
                    response = self._submit_live_order(order_params, fyers_symbol, new_order)
                except Exception as e:
                    new_order.status = "ERROR"
                    new_order.reject_reason = str(e)
                    response = {"status": "ERROR", "message": str(e)}
            else:
                paper_fill = self._simulate_paper_fill(order_params, db_symbol)
                response = self._apply_paper_fill(new_order, new_order.internal_id, paper_fill)

            db.commit()
            return response

        except Exception as e:
            logger.exception(f"Order Placement Failed: {e}")
            db.rollback()
            return {"status": "ERROR", "message": str(e)}

    def modify_order(self, order_id: str, params: dict[str, Any], db: Session) -> dict[str, Any]:
        """Modify an existing order"""
        order = db.query(LiveOrder).filter(LiveOrder.id == order_id).first()
        if not order:
            return {"status": "ERROR", "message": "Order not found"}

        if not order.is_paper:
            # Send the full resulting order so an unchanged type and trigger are
            # preserved (an SL stays an SL) instead of collapsing to a LIMIT.
            broker_params = {
                "order_type": str(params.get("order_type") or order.order_type or "MARKET").upper(),
                "quantity": params.get("quantity", order.quantity),
                "price": params.get("price", order.price),
                "trigger_price": params.get("trigger_price", order.trigger_price),
            }
            try:
                resp = self.broker.modify_order(order_id, broker_params)
                status = resp.get("s", "error")
                msg = resp.get("message", "Modify failed")

                if status == "ok":
                    order.order_type = broker_params["order_type"]
                    order.quantity = broker_params["quantity"]
                    order.price = safe_float(broker_params["price"], 0.0)
                    order.trigger_price = safe_float(broker_params["trigger_price"], 0.0)
                    order.broker_message = f"Modified: {msg}"
                    db.commit()
                    return {"status": "SUCCESS", "message": "Order Modified"}
                else:
                    return {"status": "ERROR", "message": msg}
            except Exception as e:
                return {"status": "ERROR", "message": str(e)}

        # Paper Mode Modify
        if "quantity" in params:
            try:
                quantity = int(params["quantity"])
            except (TypeError, ValueError):
                return {"status": "ERROR", "message": "Quantity must be a positive integer"}
            if quantity <= 0:
                return {"status": "ERROR", "message": "Quantity must be a positive integer"}
            params["quantity"] = quantity

        if "price" in params:
            try:
                price = float(params["price"])
            except (TypeError, ValueError):
                return {"status": "ERROR", "message": "Price must be a positive number"}
            if price <= 0:
                return {"status": "ERROR", "message": "Price must be a positive number"}
            params["price"] = price

        if "trigger_price" in params:
            trigger_price = safe_float(params["trigger_price"], -1.0)
            if trigger_price <= 0:
                return {"status": "ERROR", "message": "Trigger price must be a positive number"}
            params["trigger_price"] = trigger_price

        if order.status in PAPER_TERMINAL_STATES:
            return {"status": "ERROR", "message": f"Cannot modify order in {order.status} state"}

        # Validate the order as it will be after the change, like live modify does.
        order_type = str(params.get("order_type") or order.order_type or "MARKET").upper()
        price = safe_float(params.get("price", order.price), 0.0)
        trigger_price = safe_float(params.get("trigger_price", order.trigger_price), 0.0)
        error = order_price_error(order_type, price, trigger_price)
        if error:
            return {"status": "ERROR", "message": error}

        order.order_type = order_type
        if "quantity" in params:
            order.quantity = params["quantity"]
        order.price = price
        order.trigger_price = trigger_price
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

        if not order.is_paper:
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

        # Paper Mode
        if order.status in PAPER_TERMINAL_STATES:
            return {"status": "ERROR", "message": f"Cannot cancel order in {order.status} state"}
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
