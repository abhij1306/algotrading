"""The Fyers order and read calls HAT makes, with the circuit breaker and the guarded timeout
(K2; cut down from the legacy `FyersBroker`, HAT-BRK-081, HAT-DEC-BD2).

Every SDK call goes through `_call_with_guard`: a worker-thread call with a timeout, and a circuit
breaker that opens after three failures in a row and stays open for 20 s. Nothing is sent while the
circuit is open, and a lost answer to a place is `SUBMIT_UNCONFIRMED`, never a rejection: the order
may have landed. Reads (`get_positions`, `fetch_*`) fail closed: they raise `FyersUnavailableError`
when the broker cannot answer, never an empty list.

The breaker is an object, injected per broker, not class state (a failure recorded through one
subclass used to shadow another's). Production shares one (`PROCESS_BREAKER`) because every broker
instance talks to the same Fyers account.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Lock
from typing import Any, TypedDict

from .fyers_session import get_fyers_session

__all__ = [
    "BROKER_CIRCUIT_OPEN",
    "BROKER_ORDER_UNCONFIRMED",
    "CLIENT_NOT_INITIALIZED",
    "FYERS_ORDER_TAG_PATTERN",
    "FYERS_ORDER_TYPES",
    "GTT_PRODUCTS",
    "PROCESS_BREAKER",
    "SUBMIT_UNCONFIRMED",
    "CircuitBreaker",
    "FundsReply",
    "FyersBroker",
    "FyersUnavailableError",
    "OrderReply",
    "PositionReply",
]

logger = logging.getLogger(__name__)

CLIENT_NOT_INITIALIZED = "Client not initialized"
# OrderReply status when the order may have reached the broker but no reply confirmed it
# (timeout, transport error, malformed reply). It is neither placed nor rejected.
SUBMIT_UNCONFIRMED = "SUBMIT_UNCONFIRMED"
BROKER_ORDER_UNCONFIRMED = "Order sent but not confirmed by the broker"
BROKER_CIRCUIT_OPEN = "Broker unavailable; order not sent"

# Fyers order "type": 1 = LIMIT, 2 = MARKET, 3 = SL-M (stop market), 4 = SL (stop limit)
FYERS_ORDER_TYPES = {"LIMIT": 1, "MARKET": 2, "SL-M": 3, "SL": 4}
STOP_ORDER_TYPES = {"SL", "SL-M"}
LIMIT_PRICED_ORDER_TYPES = {"LIMIT", "SL"}
# Fyers accepts only an alphanumeric orderTag of limited length; 20 characters is the limit.
FYERS_ORDER_TAG_PATTERN = re.compile(r"^[A-Za-z0-9]{1,20}$")
# Product types the Fyers GTT endpoint accepts.
GTT_PRODUCTS = {"CNC", "MARGIN", "MTF"}
# Every Fyers product is sent as itself (MIS is INTRADAY), so a product never goes out as another.
PRODUCT_MAP = {
    "MIS": "INTRADAY",
    "INTRADAY": "INTRADAY",
    "CNC": "CNC",
    "MARGIN": "MARGIN",
    "MTF": "MTF",
    "CO": "CO",
    "BO": "BO",
}
_GTT_REFUSED = "A GTT order requires a positive trigger and limit price."


class OrderReply(TypedDict):
    order_id: str
    status: str
    message: str
    details: dict[str, Any] | None


class PositionReply(TypedDict):
    symbol: str
    side: str  # LONG/SHORT
    quantity: int
    entry_price: float
    current_price: float
    pnl: float
    product_type: str


class FundsReply(TypedDict):
    available: float
    used: float
    total: float


class FyersUnavailableError(Exception):
    """The broker client is missing or a read call failed (an outage, never an empty book)."""


def _float(value: object, default: float = 0.0) -> float:
    """`value` as a finite float, else `default` (NaN and infinity never pass as a price)."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _rejected(message: str, details: dict[str, Any] | None = None) -> OrderReply:
    return OrderReply(order_id="", status="REJECTED", message=message, details=details)


def _unconfirmed() -> OrderReply:
    return OrderReply(
        order_id="", status=SUBMIT_UNCONFIRMED, message=BROKER_ORDER_UNCONFIRMED, details=None
    )


def _order_tag_fields(order_tag: object) -> tuple[dict[str, str], OrderReply | None]:
    """The orderTag payload field for an optional tag, or a rejection Fyers would issue."""
    if order_tag is None:
        return {}, None
    if not FYERS_ORDER_TAG_PATTERN.fullmatch(str(order_tag)):
        return {}, _rejected(
            "Invalid order tag. Expected 1-20 letters or digits.", {"order_tag": order_tag}
        )
    return {"orderTag": str(order_tag)}, None


class CircuitBreaker:
    """Opens after `threshold` failures in a row and stays open for `recovery_sec`; one success
    closes it. Thread-safe."""

    def __init__(
        self,
        threshold: int = 3,
        recovery_sec: float = 20.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.recovery_sec = recovery_sec
        self._clock = clock
        self._lock = Lock()
        self.failures = 0
        self.open_until = 0.0

    def is_open(self) -> bool:
        with self._lock:
            return self._clock() < self.open_until

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.open_until = 0.0

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.open_until = self._clock() + self.recovery_sec


PROCESS_BREAKER = CircuitBreaker()
"""The breaker the production brokers share: one Fyers account, one outage."""

_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="fyers-broker")


class FyersBroker:
    """Order writes, GTT writes and fail-closed reads over the Fyers SDK model.

    `client` returns the SDK model (resolved on each use, so a re-login is picked up) or None when
    there is no usable session; the default asks the process-wide `FyersSession`.
    """

    def __init__(
        self,
        client: Callable[[], Any] | None = None,
        *,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._client_source = client if client is not None else lambda: get_fyers_session().fyers
        self.breaker = breaker if breaker is not None else CircuitBreaker()

    @property
    def client(self) -> Any:  # noqa: ANN401 - the untyped fyers_apiv3 FyersModel
        """Current Fyers SDK model, or None when there is no usable session."""
        try:
            return self._client_source()
        except Exception as error:
            logger.warning("Fyers broker not connected (%s)", type(error).__name__)
            return None

    def _call_with_guard(
        self,
        operation: str,
        fn: Callable[[], object],
        *,
        timeout_sec: float = 2.0,
        fallback: object,
    ) -> object:
        """`fn` on a worker thread with a timeout; `fallback` when the circuit is open, the call
        times out or raises. Each timeout or failure counts toward opening the circuit."""
        if self.breaker.is_open():
            return fallback

        future = _EXECUTOR.submit(fn)
        try:
            result = future.result(timeout=timeout_sec)
            self.breaker.record_success()
            return result
        except FutureTimeoutError:
            future.cancel()
            self.breaker.record_failure()
            logger.warning("Fyers operation %s timed out after %.2fs", operation, timeout_sec)
            return fallback
        except Exception as error:
            self.breaker.record_failure()
            future.cancel()
            logger.exception("Fyers operation %s failed", operation, exc_info=error)
            return fallback

    # --- writes ---------------------------------------------------------------------------

    def place_order(self, order: dict[str, Any]) -> OrderReply:
        """Place an order: {symbol (Fyers format), quantity, side, type, price, trigger_price,
        product, order_tag}. A lost answer is SUBMIT_UNCONFIRMED, never a rejection."""
        if not self.client:
            return _rejected(f"Fyers {CLIENT_NOT_INITIALIZED}. Please Login.")
        side = order.get("side")
        if side not in {"BUY", "SELL"}:
            return _rejected("Invalid order side. Expected BUY or SELL.", {"side": side})

        order_type = str(order.get("type") or "MARKET").upper()
        if order_type not in FYERS_ORDER_TYPES:
            return _rejected(
                "Invalid order type. Expected MARKET, LIMIT, SL or SL-M.",
                {"type": order.get("type")},
            )

        trigger_price = _float(order.get("trigger_price"))
        if order_type in STOP_ORDER_TYPES and trigger_price <= 0:
            return _rejected(
                f"{order_type} order requires a positive trigger price.",
                {"type": order_type, "trigger_price": order.get("trigger_price")},
            )
        limit_price = _float(order.get("price"))
        if order_type in LIMIT_PRICED_ORDER_TYPES and limit_price <= 0:
            return _rejected(
                f"{order_type} order requires a positive limit price.",
                {"type": order_type, "price": order.get("price")},
            )
        tag_fields, tag_rejection = _order_tag_fields(order.get("order_tag"))
        if tag_rejection is not None:
            return tag_rejection

        product = str(order.get("product", "INTRADAY")).upper()
        data = {
            "symbol": order["symbol"],
            "qty": order["quantity"],
            "type": FYERS_ORDER_TYPES[order_type],
            "side": 1 if side == "BUY" else -1,
            "productType": PRODUCT_MAP.get(product, "INTRADAY"),
            "limitPrice": limit_price if order_type in LIMIT_PRICED_ORDER_TYPES else 0,
            "stopPrice": trigger_price if order_type in STOP_ORDER_TYPES else 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            **tag_fields,
        }
        if self.breaker.is_open():
            # Nothing is sent while the circuit is open: a definite rejection.
            return _rejected(BROKER_CIRCUIT_OPEN)
        return self._send("place_order", lambda: self.client.place_order(data=data), "Order Placed")

    def _send(self, operation: str, call: Callable[[], object], default_message: str) -> OrderReply:
        """A place whose id comes back in `id`: SUBMITTED with an id, REJECTED on an explicit
        error, and SUBMIT_UNCONFIRMED for anything else (the order may have landed)."""
        no_reply: dict[str, Any] = {}
        try:
            response = self._call_with_guard(operation, call, timeout_sec=1.5, fallback=no_reply)
        except Exception:
            logger.exception("Fyers %s failed", operation)
            return _unconfirmed()
        if response is no_reply or not isinstance(response, dict):
            return _unconfirmed()  # timed out or raised mid-request: it may still have landed
        if response.get("s") == "ok":
            order_id = response.get("id")
            if not order_id:
                logger.error("Fyers accepted %s without returning its id", operation)
                return _unconfirmed()
            return OrderReply(
                order_id=str(order_id),
                status="SUBMITTED",
                message=response.get("message", default_message),
                details=response,
            )
        if response.get("s") != "error":
            # Neither ok nor an explicit rejection: the order may have landed.
            logger.error("Fyers %s reply was neither ok nor an error", operation)
            return _unconfirmed()
        return _rejected(response.get("message", "Fyers rejected the order"), response)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        if not self.client:
            return {"status": "ERROR", "message": CLIENT_NOT_INITIALIZED}
        data = {"id": order_id}
        return self._write(
            "cancel_order",
            lambda: self.client.cancel_order(data=data),
            "Fyers cancel timeout/unavailable",
        )

    def modify_order(self, order_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Modify a pending order. params: {order_type, quantity, price, trigger_price} describing
        the order after the change. order_type is required so the Fyers type is never guessed."""
        if not self.client:
            return {"status": "ERROR", "message": CLIENT_NOT_INITIALIZED}

        order_type = str(params.get("order_type") or "").upper()
        if order_type not in FYERS_ORDER_TYPES:
            return {
                "status": "REJECTED",
                "message": "Invalid order type. Expected MARKET, LIMIT, SL or SL-M.",
            }
        trigger_price = _float(params.get("trigger_price"))
        if order_type in STOP_ORDER_TYPES and trigger_price <= 0:
            return {
                "status": "REJECTED",
                "message": f"{order_type} order requires a positive trigger price.",
            }
        limit_price = _float(params.get("price"))
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
        return self._write(
            "modify_order",
            lambda: self.client.modify_order(data=data),
            "Fyers modify timeout/unavailable",
        )

    def _write(self, operation: str, call: Callable[[], object], timeout_message: str) -> Any:  # noqa: ANN401
        return self._call_with_guard(
            operation,
            call,
            timeout_sec=1.5,
            fallback={"s": "error", "message": timeout_message},
        )

    # --- GTT writes -----------------------------------------------------------------------

    @staticmethod
    def _gtt_refusal(side: object, product: str, trigger: float, limit_price: float) -> str | None:
        if side not in {"BUY", "SELL"}:
            return "Invalid order side. Expected BUY or SELL."
        if product not in GTT_PRODUCTS:
            return "Invalid GTT product. Expected CNC, MARGIN or MTF."
        if trigger <= 0 or limit_price <= 0:
            return _GTT_REFUSED
        return None

    def place_gtt(self, gtt: dict[str, Any]) -> OrderReply:
        """Place a single-leg GTT order. gtt: {symbol (Fyers format), quantity, side, product,
        trigger_price, price}. The SDK payload is `{side, symbol, productType, orderInfo: {leg1:
        {price, triggerPrice, qty}}}`. Like place_order, a lost answer is SUBMIT_UNCONFIRMED."""
        side = gtt.get("side")
        product = str(gtt.get("product") or "").upper()
        trigger = _float(gtt.get("trigger_price"))
        limit_price = _float(gtt.get("price"))
        reason = self._gtt_refusal(side, product, trigger, limit_price)
        if reason is not None:
            return _rejected(reason)
        if not self.client:
            return _rejected(f"Fyers {CLIENT_NOT_INITIALIZED}. Please Login.")
        if self.breaker.is_open():
            return _rejected(BROKER_CIRCUIT_OPEN)
        data = {
            "side": 1 if side == "BUY" else -1,
            "symbol": gtt["symbol"],
            "productType": product,
            "orderInfo": {
                "leg1": {"price": limit_price, "triggerPrice": trigger, "qty": gtt["quantity"]}
            },
        }
        return self._send("place_gtt", lambda: self.client.place_gtt_order(data=data), "GTT Placed")

    def modify_gtt(self, gtt_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Modify a GTT order. params: {quantity, price, trigger_price} after the change."""
        if not self.client:
            return {"status": "ERROR", "message": CLIENT_NOT_INITIALIZED}
        trigger = _float(params.get("trigger_price"))
        limit_price = _float(params.get("price"))
        if trigger <= 0 or limit_price <= 0:
            return {"status": "REJECTED", "message": _GTT_REFUSED}
        leg: dict[str, Any] = {"price": limit_price, "triggerPrice": trigger}
        if params.get("quantity") is not None:
            leg["qty"] = params["quantity"]
        data = {"id": gtt_id, "orderInfo": {"leg1": leg}}
        return self._write(
            "modify_gtt",
            lambda: self.client.modify_gtt_order(data=data),
            "Fyers modify timeout/unavailable",
        )

    def cancel_gtt(self, gtt_id: str) -> dict[str, Any]:
        if not self.client:
            return {"status": "ERROR", "message": CLIENT_NOT_INITIALIZED}
        data = {"id": gtt_id}
        return self._write(
            "cancel_gtt",
            lambda: self.client.cancel_gtt_order(data=data),
            "Fyers cancel timeout/unavailable",
        )

    # --- reads (fail closed) --------------------------------------------------------------

    def _read_ok(self, operation: str, call: Callable[[], object]) -> dict[str, Any]:
        """A Fyers read that must have answered `{"s": "ok"}`.

        Raises FyersUnavailableError when the client is missing, the call timed out or raised, the
        circuit is open, or the reply is not ok, so a caller never reads an outage as an empty book
        (trading-safety rule 7). The error text is fixed, never the broker's own.
        """
        if not self.client:
            raise FyersUnavailableError(f"Fyers {CLIENT_NOT_INITIALIZED}")
        response = self._call_with_guard(operation, call, timeout_sec=2.0, fallback=None)
        if not isinstance(response, dict) or response.get("s") != "ok":
            raise FyersUnavailableError(f"Fyers {operation} request failed")
        return response

    def get_positions(self) -> list[PositionReply]:
        """Open positions (net quantity other than zero); raises when they cannot be read."""
        response = self._read_ok("positions", lambda: self.client.positions())
        positions = []
        for p in response.get("netPositions", []):
            qty = p.get("netQty", 0)
            if qty == 0:
                continue
            side = "LONG" if qty > 0 else "SHORT"
            positions.append(
                PositionReply(
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

    def fetch_funds(self) -> FundsReply:
        """Funds; raises when they cannot be read."""
        available, used, total = self._parse_funds_response(
            self._read_ok("funds", lambda: self.client.funds())
        )
        return FundsReply(available=available, used=used, total=total)

    @staticmethod
    def _parse_funds_response(response: dict[str, Any]) -> tuple[float, float, float]:
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
