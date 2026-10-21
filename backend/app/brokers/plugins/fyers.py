import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Lock
from typing import TYPE_CHECKING, Any

from ...services.fyers_client import get_fyers_client
from ...utils.helpers import safe_float
from ..base import (
    BrokerFunds,
    BrokerUnavailableError,
    IBroker,
    OrderResponse,
    Position,
    WorkingOrder,
)

if TYPE_CHECKING:
    from fyers_apiv3.fyersModel import FyersModel

CLIENT_NOT_INITIALIZED = "Client not initialized"
# OrderResponse status when the order may have reached the broker but no reply confirmed it
# (timeout, transport error, malformed reply). It is neither placed nor rejected.
SUBMIT_UNCONFIRMED = "SUBMIT_UNCONFIRMED"
BROKER_ORDER_UNCONFIRMED = "Order sent but not confirmed by the broker"
BROKER_CIRCUIT_OPEN = "Broker unavailable; order not sent"
logger = logging.getLogger(__name__)

# Fyers order "type": 1 = LIMIT, 2 = MARKET, 3 = SL-M (stop market), 4 = SL (stop limit)
FYERS_ORDER_TYPES = {"LIMIT": 1, "MARKET": 2, "SL-M": 3, "SL": 4}
STOP_ORDER_TYPES = {"SL", "SL-M"}
LIMIT_PRICED_ORDER_TYPES = {"LIMIT", "SL"}
# Fyers accepts only an alphanumeric orderTag of limited length; 20 characters stays
# inside the documented limit with room to spare.
FYERS_ORDER_TAG_PATTERN = re.compile(r"^[A-Za-z0-9]{1,20}$")
ORDER_TAG_PREFIX = "ST"
# Product types the Fyers GTT endpoint accepts.
GTT_PRODUCTS = {"CNC", "MARGIN", "MTF"}
# Fyers orderbook statuses for an order that can still fill: 4 = transit, 6 = pending.
FYERS_WORKING_ORDER_STATUSES = {4, 6}
# Fyers orderbook status -> our order status; working states (4, 6) read as SUBMITTED.
FYERS_ORDER_STATUSES = {1: "CANCELLED", 2: "FILLED", 4: "SUBMITTED", 5: "REJECTED", 6: "SUBMITTED"}


def _order_tag_fields(order_tag: object) -> tuple[dict[str, str], OrderResponse | None]:
    """The orderTag payload field for an optional tag, or a rejection Fyers would issue."""
    if order_tag is None:
        return {}, None
    if not FYERS_ORDER_TAG_PATTERN.fullmatch(str(order_tag)):
        return {}, OrderResponse(
            order_id="",
            status="REJECTED",
            message="Invalid order tag. Expected 1-20 letters or digits.",
            details={"order_tag": order_tag},
        )
    return {"orderTag": str(order_tag)}, None


def fyers_order_tag(client_order_id: str) -> str:
    """Fyers orderTag for a client_order_id, so a broker order can be matched to its row.

    "ST" plus the first 18 hex digits of the UUID, uppercased: 20 alphanumeric characters.
    """
    hex_digits = client_order_id.replace("-", "")[:18].upper()
    return f"{ORDER_TAG_PREFIX}{hex_digits}"


def quote_last_price(quote: dict[str, Any] | None) -> float:
    """Last traded price from a FyersBroker.get_quote() result ({"v": {"lp": ...}}).

    Returns 0.0 when the quote is empty or carries no usable price.
    """
    if not isinstance(quote, dict):
        return 0.0
    values = quote.get("v")
    if not isinstance(values, dict):
        return 0.0
    price = safe_float(values.get("lp"), 0.0)
    return price if price > 0 else 0.0


class FyersBroker(IBroker):
    """
    Fyers Implementation of IBroker.
    Wraps actual API calls for Live Trading and standardizes output.
    """

    _executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="fyers-broker")
    _circuit_lock = Lock()
    _circuit_failures = 0
    _circuit_open_until = 0.0
    _circuit_failure_threshold = 3
    _circuit_recovery_sec = 20.0

    @classmethod
    def _circuit_open(cls) -> bool:
        with cls._circuit_lock:
            return time.monotonic() < cls._circuit_open_until

    @classmethod
    def _record_success(cls) -> None:
        with cls._circuit_lock:
            cls._circuit_failures = 0
            cls._circuit_open_until = 0.0

    @classmethod
    def _record_failure(cls) -> None:
        with cls._circuit_lock:
            cls._circuit_failures += 1
            if cls._circuit_failures >= cls._circuit_failure_threshold:
                cls._circuit_open_until = time.monotonic() + cls._circuit_recovery_sec

    def _call_with_guard(
        self,
        operation: str,
        fn: Callable[[], object],
        *,
        timeout_sec: float = 2.0,
        fallback: object,
    ) -> object:
        if self._circuit_open():
            return fallback

        future = self._executor.submit(fn)
        try:
            result = future.result(timeout=timeout_sec)
            self._record_success()
            return result
        except FutureTimeoutError as exc:
            future.cancel()
            self._record_failure()
            logger.warning(
                "Fyers operation %s timed out after %.2fs: %s",
                operation,
                timeout_sec,
                exc,
            )
            return fallback
        except Exception as exc:
            self._record_failure()
            future.cancel()
            logger.exception("Fyers operation %s failed", operation, exc_info=exc)
            return fallback

    @property
    def client(self) -> "FyersModel | None":
        """Current Fyers SDK model, resolved on each use so re-login is picked up."""
        try:
            return get_fyers_client().fyers
        except Exception as e:
            logger.warning("Fyers Broker: Not connected (%s)", e)
            return None

    def place_order(self, order: dict[str, Any]) -> OrderResponse:
        """
        Execute order on Fyers.
        """
        if not self.client:
            return OrderResponse(
                order_id="",
                status="REJECTED",
                message=f"Fyers {CLIENT_NOT_INITIALIZED}. Please Login.",
                details=None,
            )

        # Map generic order dict to the broker order payload.

        # Every Fyers product type is sent as itself (MIS is INTRADAY), so an MTF, CO or BO
        # order or exit never goes out as a different product. Fyers itself rejects a CO or
        # BO without its stop legs.
        product_map = {
            "MIS": "INTRADAY",
            "INTRADAY": "INTRADAY",
            "CNC": "CNC",
            "MARGIN": "MARGIN",
            "MTF": "MTF",
            "CO": "CO",
            "BO": "BO",
        }
        side = order.get("side")
        if side not in {"BUY", "SELL"}:
            return OrderResponse(
                order_id="",
                status="REJECTED",
                message="Invalid order side. Expected BUY or SELL.",
                details={"side": side},
            )

        order_type = str(order.get("type") or "MARKET").upper()
        if order_type not in FYERS_ORDER_TYPES:
            return OrderResponse(
                order_id="",
                status="REJECTED",
                message="Invalid order type. Expected MARKET, LIMIT, SL or SL-M.",
                details={"type": order.get("type")},
            )

        trigger_price = safe_float(order.get("trigger_price"), 0.0)
        if order_type in STOP_ORDER_TYPES and trigger_price <= 0:
            return OrderResponse(
                order_id="",
                status="REJECTED",
                message=f"{order_type} order requires a positive trigger price.",
                details={"type": order_type, "trigger_price": order.get("trigger_price")},
            )

        limit_price = safe_float(order.get("price"), 0.0)
        if order_type in LIMIT_PRICED_ORDER_TYPES and limit_price <= 0:
            return OrderResponse(
                order_id="",
                status="REJECTED",
                message=f"{order_type} order requires a positive limit price.",
                details={"type": order_type, "price": order.get("price")},
            )

        tag_fields, tag_rejection = _order_tag_fields(order.get("order_tag"))
        if tag_rejection is not None:
            return tag_rejection

        data = {
            "symbol": order["symbol"],
            "qty": order["quantity"],
            "type": FYERS_ORDER_TYPES[order_type],
            "side": 1 if side == "BUY" else -1,
            "productType": product_map.get(order.get("product", "INTRADAY").upper(), "INTRADAY"),
            "limitPrice": limit_price if order_type in LIMIT_PRICED_ORDER_TYPES else 0,
            "stopPrice": trigger_price if order_type in STOP_ORDER_TYPES else 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            **tag_fields,
        }

        if self._circuit_open():
            # Nothing is sent while the circuit is open: a definite rejection.
            return OrderResponse(
                order_id="", status="REJECTED", message=BROKER_CIRCUIT_OPEN, details=None
            )

        unconfirmed = OrderResponse(
            order_id="", status=SUBMIT_UNCONFIRMED, message=BROKER_ORDER_UNCONFIRMED, details=None
        )
        no_reply: dict[str, Any] = {}
        try:
            response = self._call_with_guard(
                "place_order",
                lambda: self.client.place_order(data=data),
                timeout_sec=1.5,
                fallback=no_reply,
            )
            if response is no_reply:
                # Timed out or raised mid-request: the order may still have been placed.
                return unconfirmed
            if response.get("s") == "ok":
                if not response.get("id"):
                    logger.error("Fyers accepted an order without returning its id")
                    return unconfirmed
                return OrderResponse(
                    order_id=str(response["id"]),
                    status="SUBMITTED",
                    message=response.get("message", "Order Placed"),
                    details=response,
                )
            return OrderResponse(
                order_id="",
                status="REJECTED",
                message=response.get("message", "Fyers rejected order"),
                details=response,
            )
        except Exception:
            logger.exception("Fyers place_order failed")
            return unconfirmed

    def find_order_by_tag(self, order_tag: str) -> dict[str, Any] | None:
        """The orderbook entry carrying `order_tag` as {order_id, status}, else None.

        Raises BrokerUnavailableError when the orderbook cannot be read.
        """
        if not self.client:
            raise BrokerUnavailableError(f"Fyers {CLIENT_NOT_INITIALIZED}")
        response = self._call_with_guard(
            "orderbook",
            lambda: self.client.orderbook(),
            timeout_sec=2.0,
            fallback={"s": "error"},
        )
        if not isinstance(response, dict) or response.get("s") != "ok":
            raise BrokerUnavailableError("Fyers orderbook request failed")
        for order in response.get("orderBook") or []:
            # Fyers may echo the tag with a "<n>:" prefix.
            if str(order.get("orderTag") or "").split(":")[-1] == order_tag and order.get("id"):
                return {
                    "order_id": str(order["id"]),
                    "status": FYERS_ORDER_STATUSES.get(int(order.get("status") or 0), "SUBMITTED"),
                }
        return None

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        if not self.client:
            return {"status": "ERROR", "message": CLIENT_NOT_INITIALIZED}
        data = {"id": order_id}
        return self._call_with_guard(
            "cancel_order",
            lambda: self.client.cancel_order(data=data),
            timeout_sec=1.5,
            fallback={"s": "error", "message": "Fyers cancel timeout/unavailable"},
        )

    def modify_order(self, order_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Modify pending order.
        params: {order_type, quantity, price, trigger_price} describing the order
        after the change. order_type is required so the Fyers type is never guessed.
        """
        if not self.client:
            return {"status": "ERROR", "message": CLIENT_NOT_INITIALIZED}

        order_type = str(params.get("order_type") or "").upper()
        if order_type not in FYERS_ORDER_TYPES:
            return {
                "status": "REJECTED",
                "message": "Invalid order type. Expected MARKET, LIMIT, SL or SL-M.",
            }

        trigger_price = safe_float(params.get("trigger_price"), 0.0)
        if order_type in STOP_ORDER_TYPES and trigger_price <= 0:
            return {
                "status": "REJECTED",
                "message": f"{order_type} order requires a positive trigger price.",
            }

        limit_price = safe_float(params.get("price"), 0.0)
        if order_type in LIMIT_PRICED_ORDER_TYPES and limit_price <= 0:
            return {
                "status": "REJECTED",
                "message": f"{order_type} order requires a positive limit price.",
            }

        data: dict[str, Any] = {
            "id": order_id,
            "type": FYERS_ORDER_TYPES[order_type],
            "limitPrice": limit_price if order_type in LIMIT_PRICED_ORDER_TYPES else 0,
            "stopPrice": trigger_price if order_type in STOP_ORDER_TYPES else 0,
        }
        if params.get("quantity") is not None:
            data["qty"] = params["quantity"]

        return self._call_with_guard(
            "modify_order",
            lambda: self.client.modify_order(data=data),
            timeout_sec=1.5,
            fallback={"s": "error", "message": "Fyers modify timeout/unavailable"},
        )

    def get_positions(self) -> list[Position]:
        if not self.client:
            raise BrokerUnavailableError(f"Fyers {CLIENT_NOT_INITIALIZED}")

        response = self._call_with_guard(
            "positions",
            lambda: self.client.positions(),
            timeout_sec=2.0,
            fallback={"s": "error", "message": "Fyers positions timeout/unavailable"},
        )
        # Response: {"s": "ok", "netPositions": [...]}
        if not isinstance(response, dict) or response.get("s") != "ok":
            message = response.get("message") if isinstance(response, dict) else None
            raise BrokerUnavailableError(message or "Fyers positions request failed")

        positions = []
        for p in response.get("netPositions", []):
            # Fyers side: 1 (Long), -1 (Short)
            qty = p.get("netQty", 0)
            if qty == 0:
                continue

            side = "LONG" if qty > 0 else "SHORT"

            positions.append(
                Position(
                    symbol=p.get("symbol"),
                    side=side,
                    quantity=abs(qty),
                    entry_price=float(p.get("buyAvg", 0))
                    if side == "LONG"
                    else float(p.get("sellAvg", 0)),
                    current_price=float(p.get("ltp", 0)),
                    pnl=float(p.get("pl", 0)),
                    product_type=p.get("productType", "INTRADAY"),
                )
            )
        return positions

    def get_orders(self) -> list[dict[str, Any]]:
        if not self.client:
            return []
        response = self._call_with_guard(
            "orderbook",
            lambda: self.client.orderbook(),
            timeout_sec=2.0,
            fallback={"orderBook": []},
        )
        return response.get("orderBook", [])

    def _read_ok(self, operation: str, call: Callable[[], object]) -> dict[str, Any]:
        """A Fyers read that must have answered `{"s": "ok"}`.

        Raises BrokerUnavailableError when the client is missing, the call timed out or raised,
        the circuit is open, or the reply is not ok, so a caller never reads an outage as an
        empty book (trading-safety rule 7). The error text is fixed, never the broker's own.
        """
        if not self.client:
            raise BrokerUnavailableError(f"Fyers {CLIENT_NOT_INITIALIZED}")
        response = self._call_with_guard(operation, call, timeout_sec=2.0, fallback=None)
        if not isinstance(response, dict) or response.get("s") != "ok":
            raise BrokerUnavailableError(f"Fyers {operation} request failed")
        return response

    def fetch_orderbook(self) -> list[dict[str, Any]]:
        """Today's orderbook rows as Fyers returns them; raises when it cannot be read."""
        return list(
            self._read_ok("orderbook", lambda: self.client.orderbook()).get("orderBook") or []
        )

    def fetch_tradebook(self) -> list[dict[str, Any]]:
        """Today's trades; raises when the tradebook cannot be read."""
        return list(
            self._read_ok("tradebook", lambda: self.client.tradebook()).get("tradeBook") or []
        )

    def fetch_holdings(self) -> list[dict[str, Any]]:
        """Demat holdings; raises when they cannot be read."""
        return list(self._read_ok("holdings", lambda: self.client.holdings()).get("holdings") or [])

    def fetch_funds(self) -> BrokerFunds:
        """Funds; raises when they cannot be read (get_funds answers zeros instead)."""
        available, used, total = self._parse_funds_response(
            self._read_ok("funds", lambda: self.client.funds())
        )
        return BrokerFunds(available=available, used=used, total=total)

    def fetch_gtt_orders(self) -> list[dict[str, Any]]:
        """The GTT orderbook; raises when it cannot be read."""
        return list(
            self._read_ok("gtt_orderbook", lambda: self.client.gtt_orderbook()).get("orderBook")
            or []
        )

    def place_gtt(self, gtt: dict[str, Any]) -> OrderResponse:
        """Place a single-leg GTT order.

        gtt: {symbol (Fyers format), quantity, side, product, trigger_price, price}. The SDK
        payload is `{side, symbol, productType, orderInfo: {leg1: {price, triggerPrice, qty}}}`.
        Like place_order, a lost answer is SUBMIT_UNCONFIRMED, never a rejection.
        """
        side = gtt.get("side")
        product = str(gtt.get("product") or "").upper()
        trigger = safe_float(gtt.get("trigger_price"), 0.0)
        limit_price = safe_float(gtt.get("price"), 0.0)
        reason = None
        if side not in {"BUY", "SELL"}:
            reason = "Invalid order side. Expected BUY or SELL."
        elif product not in GTT_PRODUCTS:
            reason = "Invalid GTT product. Expected CNC, MARGIN or MTF."
        elif trigger <= 0 or limit_price <= 0:
            reason = "A GTT order requires a positive trigger and limit price."
        if reason is not None:
            return OrderResponse(order_id="", status="REJECTED", message=reason, details=None)
        if not self.client:
            return OrderResponse(
                order_id="",
                status="REJECTED",
                message=f"Fyers {CLIENT_NOT_INITIALIZED}. Please Login.",
                details=None,
            )
        if self._circuit_open():
            return OrderResponse(
                order_id="", status="REJECTED", message=BROKER_CIRCUIT_OPEN, details=None
            )
        data = {
            "side": 1 if side == "BUY" else -1,
            "symbol": gtt["symbol"],
            "productType": product,
            "orderInfo": {
                "leg1": {"price": limit_price, "triggerPrice": trigger, "qty": gtt["quantity"]}
            },
        }
        unconfirmed = OrderResponse(
            order_id="", status=SUBMIT_UNCONFIRMED, message=BROKER_ORDER_UNCONFIRMED, details=None
        )
        no_reply: dict[str, Any] = {}
        try:
            response = self._call_with_guard(
                "place_gtt",
                lambda: self.client.place_gtt_order(data=data),
                timeout_sec=1.5,
                fallback=no_reply,
            )
        except Exception:
            logger.exception("Fyers place_gtt failed")
            return unconfirmed
        if response is no_reply or not isinstance(response, dict):
            return unconfirmed
        if response.get("s") == "ok":
            gtt_id = response.get("id")
            if not gtt_id:
                logger.error("Fyers accepted a GTT order without returning its id")
                return unconfirmed
            return OrderResponse(
                order_id=str(gtt_id),
                status="SUBMITTED",
                message=response.get("message", "GTT Placed"),
                details=response,
            )
        if response.get("s") != "error":
            # Neither ok nor an explicit rejection: the GTT may have landed.
            logger.error("Fyers GTT reply was neither ok nor an error")
            return unconfirmed
        return OrderResponse(
            order_id="",
            status="REJECTED",
            message=response.get("message", "Fyers rejected GTT order"),
            details=response,
        )

    def modify_gtt(self, gtt_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Modify a GTT order. params: {quantity, price, trigger_price} after the change."""
        if not self.client:
            return {"status": "ERROR", "message": CLIENT_NOT_INITIALIZED}
        trigger = safe_float(params.get("trigger_price"), 0.0)
        limit_price = safe_float(params.get("price"), 0.0)
        if trigger <= 0 or limit_price <= 0:
            return {
                "status": "REJECTED",
                "message": "A GTT order requires a positive trigger and limit price.",
            }
        leg: dict[str, Any] = {"price": limit_price, "triggerPrice": trigger}
        if params.get("quantity") is not None:
            leg["qty"] = params["quantity"]
        data = {"id": gtt_id, "orderInfo": {"leg1": leg}}
        return self._call_with_guard(
            "modify_gtt",
            lambda: self.client.modify_gtt_order(data=data),
            timeout_sec=1.5,
            fallback={"s": "error", "message": "Fyers modify timeout/unavailable"},
        )

    def cancel_gtt(self, gtt_id: str) -> dict[str, Any]:
        if not self.client:
            return {"status": "ERROR", "message": CLIENT_NOT_INITIALIZED}
        data = {"id": gtt_id}
        return self._call_with_guard(
            "cancel_gtt",
            lambda: self.client.cancel_gtt_order(data=data),
            timeout_sec=1.5,
            fallback={"s": "error", "message": "Fyers cancel timeout/unavailable"},
        )

    def get_working_orders(self) -> list[WorkingOrder]:
        """Orders still working at the broker, with their unfilled quantity.

        Raises BrokerUnavailableError when the orderbook cannot be read: callers that size
        an exit from it must fail closed rather than read "no working orders".
        """
        if not self.client:
            raise BrokerUnavailableError(f"Fyers {CLIENT_NOT_INITIALIZED}")

        response = self._call_with_guard(
            "orderbook",
            lambda: self.client.orderbook(),
            timeout_sec=2.0,
            fallback={"s": "error", "message": "Fyers orderbook timeout/unavailable"},
        )
        if not isinstance(response, dict) or response.get("s") != "ok":
            raise BrokerUnavailableError("Fyers orderbook request failed")

        working: list[WorkingOrder] = []
        for order in response.get("orderBook") or []:
            if int(order.get("status") or 0) not in FYERS_WORKING_ORDER_STATUSES:
                continue
            remaining = int(order.get("qty") or 0) - int(order.get("filledQty") or 0)
            if remaining <= 0:
                continue
            side = {1: "BUY", -1: "SELL"}.get(int(order.get("side") or 0))
            if side is None:
                raise BrokerUnavailableError("Fyers orderbook has an order with no side")
            working.append(
                WorkingOrder(
                    symbol=str(order.get("symbol") or ""),
                    side=side,
                    quantity=remaining,
                    product_type=str(order.get("productType") or ""),
                )
            )
        return working

    def get_holdings(self) -> list[dict[str, Any]]:
        if not self.client:
            return []
        response = self._call_with_guard(
            "holdings",
            lambda: self.client.holdings(),
            timeout_sec=2.0,
            fallback={"holdings": []},
        )
        return response.get("holdings", [])

    def get_funds(self) -> BrokerFunds:
        if not self.client:
            return BrokerFunds(available=0, used=0, total=0)

        response = self._call_with_guard(
            "funds",
            lambda: self.client.funds(),
            timeout_sec=2.0,
            fallback={},
        )

        available, used, total = self._parse_funds_response(response)
        return BrokerFunds(available=available, used=used, total=total)

    @staticmethod
    def _parse_funds_response(response: dict[str, Any]) -> tuple[float, float, float]:
        if not isinstance(response, dict):
            return 0.0, 0.0, 0.0

        available = float(response.get("available", 0) or 0)
        used = float(response.get("used", 0) or 0)
        total = float(response.get("total", 0) or (available + used))
        funds = response.get("fund_limit", [])
        if not isinstance(funds, list):
            return available, used, total

        for fund in funds:
            title = fund.get("title")
            amount = float(fund.get("equityAmount", 0) or 0)
            if title == "Total Balance":
                total = amount
            elif title == "Available Balance":
                available = amount
            elif title == "Utilized Amount":
                used = amount
        return available, used, total

    def get_quote(self, symbol: str) -> dict[str, Any]:
        """Get live quote."""
        if not self.client:
            return {}
        data = {"symbols": symbol}  # "NSE:SBIN-EQ"
        response = self._call_with_guard(
            "quotes",
            lambda: self.client.quotes(data=data),
            timeout_sec=1.5,
            fallback={},
        )
        if response.get("s") == "ok":
            d = response.get("d", [])
            if d:
                return d[0]
        return {}

    def get_tradebook(self) -> list[dict[str, Any]]:
        """Get executed trades."""
        if not self.client:
            return []
        response = self._call_with_guard(
            "tradebook",
            lambda: self.client.tradebook(),
            timeout_sec=2.0,
            fallback={"tradeBook": []},
        )
        return response.get("tradeBook", [])

    @staticmethod
    def _build_exit_order(position: Position) -> dict[str, Any]:
        return {
            "symbol": position["symbol"],
            "quantity": position["quantity"],
            "side": "SELL" if position["side"] == "LONG" else "BUY",
            "type": "MARKET",
            "product": position["product_type"],
        }

    def exit_position(self, symbol: str) -> dict[str, Any]:
        """Exit position for a specific symbol."""
        if not self.client:
            return {"status": "ERROR", "message": CLIENT_NOT_INITIALIZED}

        try:
            positions = self.get_positions()
        except BrokerUnavailableError as exc:
            # symbol is request input; keep it out of the log line (log injection).
            logger.warning("Exit position aborted: positions unavailable (%s)", exc)
            return {
                "status": "REJECTED",
                "code": "BROKER_UNAVAILABLE",
                "message": "Broker positions unavailable; no exit order was placed",
            }

        matching_positions = [position for position in positions if position["symbol"] == symbol]
        if not matching_positions:
            return {"status": "OK", "message": "No positions found"}

        results: list[OrderResponse] = []
        has_failure = False
        for position in matching_positions:
            exit_result = self.place_order(self._build_exit_order(position))
            results.append(exit_result)
            if exit_result["status"] not in {"SUBMITTED", "SUCCESS", "OK"}:
                has_failure = True

        return {
            "status": "ERROR" if has_failure else "OK",
            "message": "One or more exit orders failed" if has_failure else "Exit orders placed",
            "results": results,
        }

    def exit_all_positions(self) -> dict[str, Any]:
        """Panic button: Exit all positions."""
        if not self.client:
            return {"status": "ERROR", "message": CLIENT_NOT_INITIALIZED}

        # Fyers API supports exiting all via exit_positions
        return self._call_with_guard(
            "exit_positions",
            lambda: self.client.exit_positions(data={}),
            timeout_sec=1.5,
            fallback={"s": "error", "message": "Fyers exit timeout/unavailable"},
        )
