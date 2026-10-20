"""LIVE exit and square-off.

Every LIVE exit goes through `LiveExitService.exit_positions`, which:

- requires a client_order_id (UUID v4) for the request; a retry with the same id returns
  the orders it already placed and never reaches the broker again;
- checks the market is open (risk_manager.pre_exit_check);
- reads the open positions and the still-working orders from the broker, and fails closed
  when either is unavailable;
- sizes each exit from the broker position less the exit quantity already working, so an
  exit can only reduce a position: never flip it or add to it (risk_manager.exit_order_check);
- persists each exit order as PENDING_SUBMIT before the broker call, through the same path
  as a placed LIVE order (OrderExecutionService.place_prechecked_live_order).

Exits are not subject to the loss, exposure, margin or order-count limits: those exist to
stop new risk, and must never trap an open position.
"""

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from ..brokers.base import Position, WorkingOrder
from ..brokers.plugins.fyers import SUBMIT_UNCONFIRMED
from ..models.live_order import LiveOrder
from .order_execution_service import (
    CLIENT_ORDER_ID_REQUIRED_MESSAGE,
    OrderExecutionService,
    normalize_client_order_id,
    order_execution_service,
)
from .risk_manager import RiskStatus, risk_manager
from .symbol_master import SymbolFormat, symbol_master

logger = logging.getLogger(__name__)

EXIT_SOURCE = "SQUARE_OFF"
BROKER_UNAVAILABLE_MESSAGE = "Broker positions unavailable; no exit order was placed"
INVALID_SYMBOL_MESSAGE = "Invalid symbol"
NO_POSITION_MESSAGE = "No open position to exit"
EXIT_WORKING_MESSAGE = "An exit order for this position is already working"
DUPLICATE_EXIT_MESSAGE = "Duplicate client_order_id: returning the existing exit orders"
EXIT_PLACED_MESSAGE = "Exit orders placed"
EXIT_FAILED_MESSAGE = "One or more exit orders failed"
EXIT_UNCONFIRMED_MESSAGE = (
    "Exit sent but not confirmed by the broker. Check the broker orderbook before squaring again."
)
# Namespace for the per-position client_order_id derived from the request's id.
EXIT_ORDER_NAMESPACE = uuid.UUID("6f1c2a58-3b7e-4d0a-9c55-0e8f4b2d7a19")
FAILED_EXIT_STATUSES = {"ERROR", "REJECTED"}


def exit_request_tag(request_id: str) -> str:
    """order_tag shared by every exit order of one request, so a retry can find them."""
    return f"exit:{request_id}"


def exit_order_client_id(request_id: str, fyers_symbol: str, product_type: str) -> str:
    """client_order_id of one position's exit order within a request.

    Deterministic, so two concurrent requests with the same id collide on the unique
    client_order_id index instead of both placing an exit.
    """
    return str(uuid.uuid5(EXIT_ORDER_NAMESPACE, f"{request_id}|{fyers_symbol}|{product_type}"))


def _error(symbol: str | None, message: str, code: str) -> dict[str, Any]:
    return {"status": "ERROR", "mode": "LIVE", "symbol": symbol, "message": message, "code": code}


def _order_summary(order: LiveOrder) -> dict[str, Any]:
    return {
        "order_id": order.id,
        "symbol": order.fyers_symbol,
        "product_type": order.product_type,
        "side": order.side,
        "quantity": order.quantity,
        "status": order.status,
    }


class LiveExitService:
    def __init__(self, execution: OrderExecutionService | None = None) -> None:
        self._execution = execution or order_execution_service

    def exit_positions(
        self,
        *,
        symbol: str | None,
        product_type: str | None,
        client_order_id: object,
        user_id: str,
        db: Session,
    ) -> dict[str, Any]:
        """Exit one symbol's LIVE position(s), or every position when `symbol` is None.

        Returns {status: SUCCESS | ERROR, mode, symbol, message, client_order_id, orders}.
        A DB failure propagates (the API answers 503); the broker is only reached after
        the exit order's row is committed.
        """
        try:
            request_id = normalize_client_order_id(client_order_id)
        except ValueError as exc:
            return _error(symbol, str(exc), "INVALID_CLIENT_ORDER_ID")
        if request_id is None:
            return _error(symbol, CLIENT_ORDER_ID_REQUIRED_MESSAGE, "CLIENT_ORDER_ID_REQUIRED")

        # Before any check: the first request may itself change their outcome.
        existing = self._existing_exit_orders(request_id, db)
        if existing:
            return {
                "status": "SUCCESS",
                "mode": "LIVE",
                "symbol": symbol,
                "message": DUPLICATE_EXIT_MESSAGE,
                "client_order_id": request_id,
                "duplicate": True,
                "orders": [_order_summary(order) for order in existing],
            }

        gate = risk_manager.pre_exit_check()
        if gate.status == RiskStatus.FAIL:
            return _error(symbol, gate.message, gate.code)

        target_symbol: str | None = None
        if symbol:
            target_symbol = self._target_db_symbol(symbol)
            if target_symbol is None:
                return _error(symbol, INVALID_SYMBOL_MESSAGE, "INVALID_SYMBOL")

        broker = self._execution.broker
        try:
            positions = broker.get_positions()
            working = broker.get_working_orders()
        except Exception:
            # Fail closed: an unknown position or working order must not size an exit.
            logger.exception("LIVE exit aborted: broker positions or orders unavailable")
            return _error(symbol, BROKER_UNAVAILABLE_MESSAGE, "BROKER_UNAVAILABLE")

        targets = [
            position
            for position in positions
            # Matched in DB format: the book shows DB symbols, the broker Fyers symbols.
            if (target_symbol is None or self._db_symbol(position["symbol"]) == target_symbol)
            and (not product_type or position["product_type"].upper() == product_type.upper())
        ]
        if not targets:
            return {
                "status": "SUCCESS",
                "mode": "LIVE",
                "symbol": symbol,
                "message": NO_POSITION_MESSAGE,
                "client_order_id": request_id,
                "duplicate": False,
                "orders": [],
            }

        orders = [
            self._exit_one(position, working, request_id, user_id, db) for position in targets
        ]
        statuses = {order["status"] for order in orders}
        if statuses & FAILED_EXIT_STATUSES:
            status, message = "ERROR", EXIT_FAILED_MESSAGE
        elif SUBMIT_UNCONFIRMED in statuses:
            # Not a failure: the exit may be live. A new click must not send another.
            status, message = SUBMIT_UNCONFIRMED, EXIT_UNCONFIRMED_MESSAGE
        else:
            status, message = "SUCCESS", EXIT_PLACED_MESSAGE
        return {
            "status": status,
            "mode": "LIVE",
            "symbol": symbol,
            "message": message,
            "client_order_id": request_id,
            "duplicate": False,
            "orders": orders,
        }

    @staticmethod
    def _existing_exit_orders(request_id: str, db: Session) -> list[LiveOrder]:
        return (
            db.query(LiveOrder)
            .filter(LiveOrder.order_tag == exit_request_tag(request_id))
            .order_by(LiveOrder.created_at)
            .all()
        )

    @staticmethod
    def _target_db_symbol(symbol: str) -> str | None:
        """DB symbol for a request symbol in DB or Fyers format, else None (invalid)."""
        if not (
            symbol_master.is_valid(symbol, SymbolFormat.DB_FORMAT)
            or symbol_master.is_valid(symbol, SymbolFormat.FYERS_FORMAT)
        ):
            return None
        return symbol_master.to_db(symbol)

    @staticmethod
    def _db_symbol(fyers_symbol: str) -> str:
        try:
            return symbol_master.to_db(fyers_symbol)
        except ValueError:
            return fyers_symbol

    def _exit_one(
        self,
        position: Position,
        working: list[WorkingOrder],
        request_id: str,
        user_id: str,
        db: Session,
    ) -> dict[str, Any]:
        fyers_symbol = position["symbol"]
        product_type = position["product_type"]
        summary: dict[str, Any] = {"symbol": fyers_symbol, "product_type": product_type}
        open_qty = int(position["quantity"])
        net_qty = open_qty if position["side"] == "LONG" else -open_qty
        side = "SELL" if net_qty > 0 else "BUY"
        # An exit already working at the broker will close this much once it fills.
        in_flight = sum(
            order["quantity"]
            for order in working
            if order["symbol"] == fyers_symbol
            and order["product_type"] == product_type
            and order["side"] == side
        )
        quantity = open_qty - in_flight
        if quantity <= 0:
            return {**summary, "status": "SKIPPED", "message": EXIT_WORKING_MESSAGE}

        check = risk_manager.exit_order_check(net_qty, side, quantity)
        if check.status == RiskStatus.FAIL:
            return {**summary, "status": "ERROR", "message": check.message, "code": check.code}

        row_id = exit_order_client_id(request_id, fyers_symbol, product_type)
        order_params: dict[str, Any] = {
            "symbol": self._db_symbol(fyers_symbol),
            "side": side,
            "quantity": quantity,
            "type": "MARKET",
            "product": product_type,
            "price": 0.0,
            "trigger_price": 0.0,
            "user_id": user_id,
            "tag": exit_request_tag(request_id),
            "source": EXIT_SOURCE,
            "instrument_type": None,
        }
        result = self._execution.place_prechecked_live_order(order_params, fyers_symbol, row_id, db)
        return {
            **summary,
            "order_id": result.get("order_id"),
            "side": side,
            "quantity": quantity,
            "status": result.get("status"),
            "message": result.get("message"),
        }


live_exit_service = LiveExitService()
