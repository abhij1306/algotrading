import logging
import uuid
from datetime import date
from decimal import Decimal
from typing import Any, Self

from pydantic import UUID4, BaseModel, model_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..brokers.plugins.fyers import (
    FYERS_ORDER_TYPES,
    SUBMIT_UNCONFIRMED,
    FyersBroker,
    fyers_order_tag,
    quote_last_price,
)
from ..models.live_order import LiveOrder
from ..services.execution import paper_book
from ..services.risk_manager import risk_manager
from ..services.symbol_master import symbol_master
from ..utils.db_errors import is_db_unavailable
from ..utils.helpers import safe_float
from ..utils.logger import safe_log_value

logger = logging.getLogger(__name__)

NO_PAPER_PRICE_MESSAGE = (
    "No market price available for {symbol}; paper {order_type} order rejected. "
    "Subscribe the symbol to live data or use a LIMIT order."
)
LIVE_SUBMIT_FAILED_MESSAGE = "Broker order submission failed"
LIVE_REJECTED_MESSAGE = "Broker rejected the order"
LIVE_MODIFY_FAILED_MESSAGE = "Broker order modify failed"
LIVE_MODIFY_REJECTED_MESSAGE = "Broker rejected the modify"
LIVE_CANCEL_FAILED_MESSAGE = "Broker order cancel failed"
LIVE_CANCEL_REJECTED_MESSAGE = "Broker rejected the cancel"
# The longest broker reason passed on to the client after a fixed message.
MAX_BROKER_REASON_LENGTH = 120
ORDER_FAILED_MESSAGE = "Order placement failed"
CLIENT_ORDER_ID_REQUIRED_MESSAGE = "LIVE order requires a client_order_id (UUID v4)"
INVALID_CLIENT_ORDER_ID_MESSAGE = "client_order_id must be a UUID v4"
DUPLICATE_ORDER_MESSAGE = "Duplicate client_order_id: returning the existing order"
# A LIVE row is committed in this state before the broker is called.
PENDING_SUBMIT = "PENDING_SUBMIT"
# The broker call failed in a way that leaves it unknown whether the order was placed.
UNCONFIRMED_MESSAGE = (
    "Order sent but not confirmed by the broker. Check the broker orderbook before "
    "placing it again."
)
# PAPER simulator defaults, overridable per order with paper_slippage_bps / paper_fee_bps.
DEFAULT_PAPER_SLIPPAGE_BPS = Decimal("5")
DEFAULT_PAPER_FEE_BPS = Decimal("3.5")
PAPER_TERMINAL_STATES = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
# A LIVE row in one of these states has no working broker order to modify.
LIVE_UNMODIFIABLE_STATES = {
    PENDING_SUBMIT,
    SUBMIT_UNCONFIRMED,
    "ERROR",
    *PAPER_TERMINAL_STATES,
}
STOP_ORDER_TYPES = {"SL", "SL-M"}
LIMIT_PRICED_ORDER_TYPES = {"LIMIT", "SL"}


def order_price_error(order_type: str, price: float, trigger_price: float) -> str | None:
    """Message when an order's prices are invalid for its type, else None.

    Same rules and wording as FyersBroker.place_order / modify_order.
    """
    if order_type in STOP_ORDER_TYPES and trigger_price <= 0:
        return f"{order_type} order requires a positive trigger price."
    if order_type in LIMIT_PRICED_ORDER_TYPES and price <= 0:
        return f"{order_type} order requires a positive limit price."
    return None


def broker_reason(response: object) -> str | None:
    """Short, printable reason from a structured broker reply's "message" field, else None.

    Only a reply the broker returned (a dict) is read; exception text never is.
    """
    if not isinstance(response, dict):
        return None
    message = response.get("message")
    if not isinstance(message, str):
        return None
    text = "".join(ch for ch in " ".join(message.split()) if ch.isprintable())
    return text[:MAX_BROKER_REASON_LENGTH] or None


def with_broker_reason(fixed_message: str, response: object) -> str:
    """The fixed client message, plus the broker's structured reason when it gave one."""
    reason = broker_reason(response)
    return f"{fixed_message}: {reason}" if reason else fixed_message


def normalize_client_order_id(value: object) -> str | None:
    """Canonical lowercase UUID v4 string for a client_order_id, or None when absent.

    Raises ValueError when the value is present but not a UUID v4.
    """
    if value is None or value == "":
        return None
    try:
        parsed = uuid.UUID(str(value))
    except ValueError as exc:
        raise ValueError(INVALID_CLIENT_ORDER_ID_MESSAGE) from exc
    if parsed.version != 4:
        raise ValueError(INVALID_CLIENT_ORDER_ID_MESSAGE)
    return str(parsed)


class ClientOrderIdField(BaseModel):
    """Order request field: the client's idempotency key, required for LIVE orders.

    The client generates one UUID v4 per submit intent and reuses it on retries.
    """

    mode: str = "PAPER"
    client_order_id: UUID4 | None = None

    @model_validator(mode="after")
    def _live_requires_client_order_id(self) -> Self:
        if self.mode.upper() == "LIVE" and self.client_order_id is None:
            raise ValueError(CLIENT_ORDER_ID_REQUIRED_MESSAGE)
        return self


def _is_marketable(side: str, limit_price: Decimal, ref: Decimal) -> bool:
    return (side == "BUY" and limit_price >= ref) or (side == "SELL" and limit_price <= ref)


def _is_triggered(side: str, trigger_price: Decimal, ref: Decimal) -> bool:
    return (side == "BUY" and trigger_price <= ref) or (side == "SELL" and trigger_price >= ref)


def _paper_fill_decision(
    order_type: str, side: str, price: Decimal, trigger_price: Decimal, ref: Decimal
) -> tuple[str | None, str]:
    """How a PAPER order fills against reference price `ref`.

    Returns (fill kind, message): the kind is "market" (fill at the slipped price),
    "limit" (slipped price capped at the limit) or None (the order rests unfilled).
    """
    if order_type == "MARKET":
        return "market", "Paper simulated market fill"
    if order_type == "LIMIT":
        if _is_marketable(side, price, ref):
            return "limit", "Paper simulated marketable limit fill"
        return None, "Paper limit resting (not marketable)"
    if order_type == "SL":
        if (
            _is_triggered(side, trigger_price, ref)
            and price > 0
            and _is_marketable(side, price, ref)
        ):
            return "limit", "Paper simulated stop-limit fill"
        return None, "Paper stop-limit pending trigger/fill"
    if order_type == "SL-M":
        if _is_triggered(side, trigger_price, ref):
            return "market", "Paper simulated stop-market fill"
        return None, "Paper stop-market pending trigger"
    return None, f"Paper unsupported order type {order_type}"


def latest_tick_price(db_symbol: str) -> float:
    """Last traded price from the live market-data tick cache, or 0.0 if none.

    This reads the market-data feed, never the order broker, so PAPER fills can
    use it without reaching the live broker. Only a fresh tick counts (market open
    and exchange time within PAPER_TICK_MAX_AGE_SEC); see LiveMarketService.get_fresh_tick.
    """
    from .live_market_service import live_market

    tick = live_market.get_fresh_tick(db_symbol)
    if tick is None:
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
    def _paper_reference_price(db_symbol: str, limit_price: Decimal) -> Decimal:
        """Reference price for a PAPER fill: the latest market-data tick, else
        the order's own limit price, else 0 (caller rejects). Never the broker."""
        try:
            tick_price = paper_book.to_decimal(latest_tick_price(db_symbol))
        except Exception:
            logger.exception("Paper reference price lookup failed for %s", db_symbol)
            tick_price = paper_book.ZERO
        if tick_price > 0:
            return tick_price
        return limit_price if limit_price > 0 else paper_book.ZERO

    def _simulate_paper_fill(self, order_params: dict[str, Any], db_symbol: str) -> dict[str, Any]:
        """Simulate a PAPER fill. Money math is Decimal (paper_book); the returned prices
        and charges are quantized to paise and given as floats for the row and the API."""
        side = str(order_params.get("side", "BUY")).upper()
        order_type = str(order_params.get("type", "MARKET")).upper()
        qty = max(0, int(order_params.get("quantity", 0)))
        price = paper_book.to_decimal(order_params.get("price"))
        trigger_price = paper_book.to_decimal(order_params.get("trigger_price"))
        slippage_bps = paper_book.to_decimal(
            order_params.get("paper_slippage_bps", DEFAULT_PAPER_SLIPPAGE_BPS),
            DEFAULT_PAPER_SLIPPAGE_BPS,
        )
        fee_bps = paper_book.to_decimal(
            order_params.get("paper_fee_bps", DEFAULT_PAPER_FEE_BPS), DEFAULT_PAPER_FEE_BPS
        )

        def outcome(
            status: str, fill: Decimal, filled_qty: int, charges: Decimal, message: str
        ) -> dict[str, Any]:
            return {
                "status": status,
                "fill_price": paper_book.money_float(fill),
                "filled_qty": filled_qty,
                "estimated_charges": paper_book.money_float(charges),
                "slippage_bps": float(slippage_bps),
                "fill_source": "paper_sim",
                "message": message,
            }

        zero = paper_book.ZERO
        price_error = order_price_error(order_type, float(price), float(trigger_price))
        if price_error:
            return outcome("REJECTED", zero, 0, zero, price_error)

        ref = self._paper_reference_price(db_symbol, price)
        if ref <= 0:
            message = NO_PAPER_PRICE_MESSAGE.format(symbol=db_symbol, order_type=order_type)
            return outcome("REJECTED", zero, 0, zero, message)

        fill_kind, reason = _paper_fill_decision(order_type, side, price, trigger_price, ref)
        if fill_kind is None:
            return outcome("SUBMITTED", zero, 0, zero, reason)

        # Slippage never fills a limit order beyond its limit price.
        limit = price if fill_kind == "limit" else None
        fill = paper_book.fill_price(side, ref, slippage_bps, limit)
        charges = paper_book.estimated_charges(fill, qty, fee_bps)
        return outcome("FILLED", fill, qty, charges, reason)

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
        client_order_id: str | None,
    ) -> LiveOrder:
        internal_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
        return LiveOrder(
            id=internal_id,
            internal_id=internal_id,
            client_order_id=client_order_id,
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
        self,
        order_params: dict[str, Any],
        fyers_symbol: str,
        new_order: LiveOrder,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        broker_order: dict[str, Any] = {
            "symbol": fyers_symbol,
            "quantity": int(order_params.get("quantity", 0)),
            "side": order_params.get("side", "BUY"),
            "type": order_params.get("type", "MARKET"),
            "product": order_params.get("product", "INTRADAY"),
            "price": safe_float(order_params.get("price"), 0.0),
            "trigger_price": safe_float(order_params.get("trigger_price"), 0.0),
        }
        if client_order_id:
            # Lets a broker order be matched to its row if our status update is lost.
            broker_order["order_tag"] = fyers_order_tag(client_order_id)
        broker_resp = self.broker.place_order(broker_order)

        if not isinstance(broker_resp, dict):
            logger.error("Broker returned non-dict response for %s", new_order.internal_id)
            return self._unconfirmed(new_order, client_order_id)

        if broker_resp.get("status") == "SUBMITTED" and broker_resp.get("order_id"):
            order_id = str(broker_resp.get("order_id"))
            message = broker_reason(broker_resp) or "Submitted"
            new_order.id = order_id
            new_order.status = "SUBMITTED"
            new_order.broker_message = message
            return {"status": "SUBMITTED", "order_id": order_id, "message": message}

        if broker_resp.get("status") == "REJECTED":
            # A structured rejection: a fixed message plus the broker's short reason.
            message = with_broker_reason(LIVE_REJECTED_MESSAGE, broker_resp)
            new_order.status = "REJECTED"
            new_order.reject_reason = message
            return {"status": "REJECTED", "message": message}

        # ERROR, SUBMIT_UNCONFIRMED, SUBMITTED without an id or an unknown status: the
        # order may have reached the broker, so it is never reported as rejected.
        logger.error(
            "Live order %s has no confirmed broker outcome: status=%s message=%s",
            new_order.internal_id,
            safe_log_value(broker_resp.get("status")),
            safe_log_value(broker_resp.get("message")),
        )
        return self._unconfirmed(new_order, client_order_id)

    def _unconfirmed(self, new_order: LiveOrder, client_order_id: str | None) -> dict[str, Any]:
        """Settle an order whose broker call gave no clear answer.

        Looks the order up once in the broker orderbook by its order tag. If found, the
        row takes the broker id and status. Otherwise it is marked SUBMIT_UNCONFIRMED
        and the client is told to check the orderbook rather than to retry.
        """
        found = None
        if client_order_id:
            try:
                found = self.broker.find_order_by_tag(fyers_order_tag(client_order_id))
            except Exception:
                logger.exception("Orderbook lookup failed for %s", new_order.internal_id)
        if found:
            order_id = str(found["order_id"])
            status = str(found["status"])
            new_order.id = order_id
            new_order.status = status
            new_order.broker_message = "Confirmed from the broker orderbook"
            logger.info("Live order %s found in the orderbook as %s", order_id, status)
            return {"status": status, "order_id": order_id, "message": new_order.broker_message}
        new_order.status = SUBMIT_UNCONFIRMED
        new_order.reject_reason = None
        new_order.broker_message = UNCONFIRMED_MESSAGE
        return {
            "status": SUBMIT_UNCONFIRMED,
            "order_id": new_order.internal_id,
            "message": UNCONFIRMED_MESSAGE,
        }

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

    def _pre_trade_error(
        self, order_params: dict[str, Any], effective_mode: str, fyers_symbol: str, db: Session
    ) -> dict[str, Any] | None:
        """Error response when a LIVE order fails its risk checks, else None. PAPER skips them."""
        if effective_mode != "LIVE":
            return None
        # Risk price estimates quote the broker, which needs the Fyers symbol.
        risk_params = {**order_params, "fyers_symbol": fyers_symbol}
        return self._run_live_risk_checks(risk_params, db)

    def _submit_live_order_safely(
        self,
        order_params: dict[str, Any],
        fyers_symbol: str,
        new_order: LiveOrder,
        client_order_id: str | None,
    ) -> dict[str, Any]:
        """Submit a LIVE order; a broker exception leaves its outcome unconfirmed."""
        try:
            return self._submit_live_order(order_params, fyers_symbol, new_order, client_order_id)
        except Exception:
            # Full error stays in the server log; the order may still have been placed,
            # so it is reconciled or marked SUBMIT_UNCONFIRMED, never REJECTED.
            logger.exception("Live order submission failed for %s", fyers_symbol)
            return self._unconfirmed(new_order, client_order_id)

    @staticmethod
    def _find_by_client_order_id(client_order_id: str | None, db: Session) -> LiveOrder | None:
        if client_order_id is None:
            return None
        return db.query(LiveOrder).filter(LiveOrder.client_order_id == client_order_id).first()

    @staticmethod
    def _duplicate_response(order: LiveOrder) -> dict[str, Any]:
        """The current state of an order already placed under the same client_order_id."""
        return {
            "order_id": order.id,
            "status": order.status,
            "message": DUPLICATE_ORDER_MESSAGE,
            "average_price": float(order.average_price or 0.0),
            "filled_qty": int(order.filled_qty or 0),
            "client_order_id": order.client_order_id,
            "duplicate": True,
        }

    @staticmethod
    def _fresh_response(response: dict[str, Any], client_order_id: str | None) -> dict[str, Any]:
        return {**response, "client_order_id": client_order_id, "duplicate": False}

    def _insert_order(
        self, new_order: LiveOrder, client_order_id: str | None, db: Session
    ) -> LiveOrder | None:
        """Commit a new order row. Returns None once it is saved, or the existing row when a
        concurrent request with the same client_order_id committed first."""
        db.add(new_order)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = self._find_by_client_order_id(client_order_id, db)
            if existing is None:
                raise
            logger.info(
                "Duplicate client_order_id %s: returning existing order %s",
                client_order_id,
                existing.id,
            )
            return existing
        return None

    def _place_live_order(
        self,
        order_params: dict[str, Any],
        fyers_symbol: str,
        new_order: LiveOrder,
        client_order_id: str,
        db: Session,
    ) -> dict[str, Any]:
        """Commit the row as PENDING_SUBMIT, then call the broker, then save its outcome.

        The broker is only called once the row is committed: if that commit fails, the
        exception propagates and no order is placed. A retry with the same client_order_id
        then finds the row and never reaches the broker again.
        """
        new_order.status = PENDING_SUBMIT
        existing = self._insert_order(new_order, client_order_id, db)
        if existing is not None:
            return self._duplicate_response(existing)

        response = self._submit_live_order_safely(
            order_params, fyers_symbol, new_order, client_order_id
        )
        broker_order_id = new_order.id if new_order.status == "SUBMITTED" else None
        try:
            db.commit()
        except Exception:
            if broker_order_id is not None:
                logger.error(
                    "LIVE order %s (client_order_id %s) accepted by broker but its status was "
                    "not saved; the row stays %s",
                    broker_order_id,
                    client_order_id,
                    PENDING_SUBMIT,
                )
            raise
        return self._fresh_response(response, client_order_id)

    def place_prechecked_live_order(
        self, order_params: dict[str, Any], fyers_symbol: str, client_order_id: str, db: Session
    ) -> dict[str, Any]:
        """Persist and submit a LIVE order whose risk checks the caller already ran.

        For exits (live_exit_service), which use their own checks: the row is committed
        as PENDING_SUBMIT before the broker call, exactly as in place_order.
        """
        quantity = self._validate_quantity(order_params)
        new_order = self._create_order_record(
            order_params,
            str(order_params["symbol"]),
            fyers_symbol,
            quantity,
            "LIVE",
            client_order_id,
        )
        return self._place_live_order(order_params, fyers_symbol, new_order, client_order_id, db)

    def _place_paper_order(
        self,
        order_params: dict[str, Any],
        db_symbol: str,
        new_order: LiveOrder,
        client_order_id: str | None,
        db: Session,
    ) -> dict[str, Any]:
        """Simulate the fill and save the row. Never reaches the broker."""
        paper_fill = self._simulate_paper_fill(order_params, db_symbol)
        response = self._apply_paper_fill(new_order, new_order.internal_id, paper_fill)
        existing = self._insert_order(new_order, client_order_id, db)
        if existing is not None:
            return self._duplicate_response(existing)
        return self._fresh_response(response, client_order_id)

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
                "tag": "manual",
                "client_order_id": "<uuid4>"  # required for LIVE, optional for PAPER
            }
            db: Database session

        Returns:
            Dict with order_id, status, message, client_order_id and duplicate. A repeated
            client_order_id returns the existing order with duplicate=True and places nothing.
        """
        try:
            effective_mode = self._resolve_mode(order_params, mode)
            client_order_id = normalize_client_order_id(order_params.get("client_order_id"))
            if effective_mode == "LIVE" and client_order_id is None:
                return {
                    "status": "ERROR",
                    "message": CLIENT_ORDER_ID_REQUIRED_MESSAGE,
                    "code": "CLIENT_ORDER_ID_REQUIRED",
                }
            # Before the risk checks: the first order may itself change their outcome.
            existing = self._find_by_client_order_id(client_order_id, db)
            if existing is not None:
                return self._duplicate_response(existing)

            quantity = self._validate_quantity(order_params)
            db_symbol, fyers_symbol = self._resolve_symbols(order_params)
            risk_error = self._pre_trade_error(order_params, effective_mode, fyers_symbol, db)
            if risk_error:
                return risk_error

            new_order = self._create_order_record(
                order_params, db_symbol, fyers_symbol, quantity, effective_mode, client_order_id
            )
            if effective_mode == "LIVE" and client_order_id is not None:
                return self._place_live_order(
                    order_params, fyers_symbol, new_order, client_order_id, db
                )
            return self._place_paper_order(order_params, db_symbol, new_order, client_order_id, db)

        except ValueError as e:
            # Request validation (mode, quantity, symbol, client_order_id); the message is
            # written for the user.
            db.rollback()
            return {"status": "ERROR", "message": str(e)}
        except Exception as e:
            db.rollback()
            if is_db_unavailable(e):
                # Fail closed: the API answers 503, never "placed". A LIVE order only reaches
                # the broker after its PENDING_SUBMIT row is committed, so a DB that is down
                # stops it first.
                raise
            logger.exception("Order placement failed")
            return {"status": "ERROR", "message": ORDER_FAILED_MESSAGE}

    def modify_order(self, order_id: str, params: dict[str, Any], db: Session) -> dict[str, Any]:
        """Modify an existing order"""
        order = db.query(LiveOrder).filter(LiveOrder.id == order_id).first()
        if not order:
            return {"status": "ERROR", "message": "Order not found"}

        if not order.is_paper:
            return self._modify_live_order(order, order_id, params, db)
        return self._modify_paper_order(order, params, db)

    def _modify_live_order(
        self, order: LiveOrder, order_id: str, params: dict[str, Any], db: Session
    ) -> dict[str, Any]:
        value_error = self._normalize_modify_params(params)
        if value_error:
            return {"status": "ERROR", "message": value_error}
        if order.status in LIVE_UNMODIFIABLE_STATES:
            return {"status": "ERROR", "message": f"Cannot modify order in {order.status} state"}

        # Send the full resulting order so an unchanged type and trigger are
        # preserved (an SL stays an SL) instead of collapsing to a LIMIT.
        broker_params = {
            "order_type": str(params.get("order_type") or order.order_type or "MARKET").upper(),
            "quantity": int(params.get("quantity", order.quantity)),
            "price": safe_float(params.get("price", order.price), 0.0),
            "trigger_price": safe_float(params.get("trigger_price", order.trigger_price), 0.0),
        }
        if broker_params["order_type"] not in FYERS_ORDER_TYPES:
            return {
                "status": "ERROR",
                "message": "Invalid order type. Expected MARKET, LIMIT, SL or SL-M.",
            }
        price_error = order_price_error(
            broker_params["order_type"], broker_params["price"], broker_params["trigger_price"]
        )
        if price_error:
            return {"status": "ERROR", "message": price_error}

        risk_error, warnings = self._live_modify_risk(order, broker_params, db)
        if risk_error:
            return risk_error

        try:
            resp = self.broker.modify_order(order_id, broker_params)
        except Exception as e:
            if is_db_unavailable(e):
                raise
            # Full error stays in the server log; the client gets a fixed message.
            logger.exception("Live modify failed for order %s", safe_log_value(order_id))
            return {"status": "ERROR", "message": LIVE_MODIFY_FAILED_MESSAGE}

        if not isinstance(resp, dict) or resp.get("s") != "ok":
            message = with_broker_reason(LIVE_MODIFY_REJECTED_MESSAGE, resp)
            return {"status": "ERROR", "message": message}
        order.order_type = broker_params["order_type"]
        order.quantity = broker_params["quantity"]
        order.price = broker_params["price"]
        order.trigger_price = broker_params["trigger_price"]
        order.broker_message = with_broker_reason("Modified", resp)
        db.commit()
        result: dict[str, Any] = {"status": "SUCCESS", "message": "Order Modified"}
        if warnings:
            result["warnings"] = warnings
        return result

    def _live_modify_risk(
        self, order: LiveOrder, broker_params: dict[str, Any], db: Session
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """(error response, warnings) for a LIVE modify. Fails closed without a live quote."""
        quote_symbol = order.fyers_symbol or order.symbol
        try:
            ltp = quote_last_price(self.broker.get_quote(quote_symbol))
        except Exception:
            logger.exception("Quote for live modify failed for %s", safe_log_value(quote_symbol))
            ltp = 0.0
        risk_params = {
            "symbol": order.symbol,
            "fyers_symbol": order.fyers_symbol,
            "side": order.side,
            "quantity": broker_params["quantity"],
            "type": broker_params["order_type"],
            "price": broker_params["price"],
            "trigger_price": broker_params["trigger_price"],
            "product": order.product_type,
            "instrument_type": order.instrument_type,
        }
        current = {
            "quantity": int(order.quantity or 0),
            "type": str(order.order_type or "MARKET").upper(),
            "price": safe_float(order.price, 0.0),
        }
        result = risk_manager.pre_modify_check(risk_params, current, ltp, db)
        if result.status.value == "FAIL":
            return {
                "status": "ERROR",
                "message": f"Risk Check Failed: {result.message}",
                "code": result.code,
            }, []
        warnings = [str(w) for w in (result.details or {}).get("warnings", [])]
        for warning in warnings:
            logger.warning("Risk warning on live modify of %s: %s", order.id, warning)
        return None, warnings

    @staticmethod
    def _normalize_modify_params(params: dict[str, Any]) -> str | None:
        """Coerce quantity, price and trigger_price in `params` in place.

        Returns an error message for the first invalid value, else None.
        """
        if "quantity" in params:
            try:
                quantity = int(params["quantity"])
            except (TypeError, ValueError):
                quantity = 0
            if quantity <= 0:
                return "Quantity must be a positive integer"
            params["quantity"] = quantity

        if "price" in params:
            try:
                price = float(params["price"])
            except (TypeError, ValueError):
                price = 0.0
            if price <= 0:
                return "Price must be a positive number"
            params["price"] = price

        if "trigger_price" in params:
            trigger_price = safe_float(params["trigger_price"], -1.0)
            if trigger_price <= 0:
                return "Trigger price must be a positive number"
            params["trigger_price"] = trigger_price
        return None

    def _modify_paper_order(
        self, order: LiveOrder, params: dict[str, Any], db: Session
    ) -> dict[str, Any]:
        value_error = self._normalize_modify_params(params)
        if value_error:
            return {"status": "ERROR", "message": value_error}

        if order.status in PAPER_TERMINAL_STATES:
            return {"status": "ERROR", "message": f"Cannot modify order in {order.status} state"}

        # Validate the order as it will be after the change, like live modify does.
        order_type = str(params.get("order_type") or order.order_type or "MARKET").upper()
        if order_type not in FYERS_ORDER_TYPES:
            return {
                "status": "ERROR",
                "message": "Invalid order type. Expected MARKET, LIMIT, SL or SL-M.",
            }
        quantity = int(params.get("quantity", order.quantity))
        price = safe_float(params.get("price", order.price), 0.0)
        trigger_price = safe_float(params.get("trigger_price", order.trigger_price), 0.0)
        error = order_price_error(order_type, price, trigger_price)
        if error:
            return {"status": "ERROR", "message": error}

        # Re-run the fill simulation exactly as on placement: a LIMIT moved through the
        # market fills now, a stop moved past the market triggers, anything else rests.
        paper_fill = self._simulate_paper_fill(
            {
                "side": order.side,
                "type": order_type,
                "quantity": quantity,
                "price": price,
                "trigger_price": trigger_price,
            },
            order.symbol,
        )
        if paper_fill["status"] == "REJECTED":
            # Like a broker refusing a modify: the working order stays as it was.
            return {"status": "ERROR", "message": paper_fill["message"]}

        order.order_type = order_type
        order.quantity = quantity
        order.price = price
        order.trigger_price = trigger_price
        fill = self._apply_paper_fill(order, order.internal_id, paper_fill)
        db.commit()
        return {
            "status": "SUCCESS",
            "message": "Paper Order Modified",
            "order_id": order.id,
            "order_status": fill["status"],
            "fill_message": fill["message"],
            "average_price": fill["average_price"],
            "filled_qty": fill["filled_qty"],
            "estimated_charges": fill["estimated_charges"],
        }

    def cancel_order(self, order_id: str, db: Session) -> dict[str, Any]:
        """Cancel an order"""
        order = self._find_order(order_id, db)
        if not order:
            return {"status": "ERROR", "message": "Order not found"}

        if not order.is_paper:
            return self._cancel_live_order(order, db)
        return self._cancel_paper_order(order, db)

    @staticmethod
    def _find_order(order_id: str, db: Session) -> LiveOrder | None:
        """Order by broker id, else by internal id."""
        order = db.query(LiveOrder).filter(LiveOrder.id == order_id).first()
        if order:
            return order
        return db.query(LiveOrder).filter(LiveOrder.internal_id == order_id).first()

    def _cancel_live_order(self, order: LiveOrder, db: Session) -> dict[str, Any]:
        try:
            resp = self.broker.cancel_order(order.id)  # Use broker ID
        except Exception as e:
            if is_db_unavailable(e):
                raise
            # Full error stays in the server log; the client gets a fixed message.
            logger.exception("Live cancel failed for order %s", safe_log_value(order.id))
            return {"status": "ERROR", "message": LIVE_CANCEL_FAILED_MESSAGE}

        if not isinstance(resp, dict):
            logger.error("Broker returned non-dict cancel response for %s", order.id)
            return {"status": "ERROR", "message": LIVE_CANCEL_FAILED_MESSAGE}
        if resp.get("s") == "ok":
            order.status = "CANCELLED"
            db.commit()
            return {"status": "SUCCESS", "message": "Order Cancelled"}
        if "already cancelled" in str(resp.get("message", "")).lower():
            order.status = "CANCELLED"
            db.commit()
            return {"status": "SUCCESS", "message": "Order Already Cancelled"}
        return {
            "status": "ERROR",
            "message": with_broker_reason(LIVE_CANCEL_REJECTED_MESSAGE, resp),
        }

    @staticmethod
    def _cancel_paper_order(order: LiveOrder, db: Session) -> dict[str, Any]:
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
