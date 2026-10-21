"""Legacy Fyers broker (IBroker). HAT owns the order, GTT and fail-closed read calls and the circuit
breaker (`app.hat.execution.fyers_broker.FyersBroker`, K2); this class inherits them and adds only
what the legacy manual-trading code uses: the lenient reads, the orderbook lookup by tag, working
orders, quotes and the exit helpers. K4 deletes it with the manual-trading module.

It shares HAT's process-wide circuit breaker, because both talk to the same Fyers account.
"""

import logging
from typing import Any

from app.hat.execution.fyers_broker import (
    BROKER_CIRCUIT_OPEN,
    BROKER_ORDER_UNCONFIRMED,
    CLIENT_NOT_INITIALIZED,
    FYERS_ORDER_TAG_PATTERN,
    FYERS_ORDER_TYPES,
    PROCESS_BREAKER,
    SUBMIT_UNCONFIRMED,
)
from app.hat.execution.fyers_broker import (
    FyersBroker as HatFyersBroker,
)

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

__all__ = [
    "BROKER_CIRCUIT_OPEN",
    "BROKER_ORDER_UNCONFIRMED",
    "CLIENT_NOT_INITIALIZED",
    "FYERS_ORDER_TAG_PATTERN",
    "FYERS_ORDER_TYPES",
    "SUBMIT_UNCONFIRMED",
    "FyersBroker",
    "fyers_order_tag",
    "quote_last_price",
]

logger = logging.getLogger(__name__)

ORDER_TAG_PREFIX = "ST"
# Fyers orderbook statuses for an order that can still fill: 4 = transit, 6 = pending.
FYERS_WORKING_ORDER_STATUSES = {4, 6}
# Fyers orderbook status -> our order status; working states (4, 6) read as SUBMITTED.
FYERS_ORDER_STATUSES = {1: "CANCELLED", 2: "FILLED", 4: "SUBMITTED", 5: "REJECTED", 6: "SUBMITTED"}


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


class FyersBroker(HatFyersBroker, IBroker):
    """Legacy Fyers implementation of IBroker over HAT's broker."""

    def __init__(self) -> None:
        super().__init__(lambda: get_fyers_client().fyers, breaker=PROCESS_BREAKER)

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

    def fetch_gtt_orders(self) -> list[dict[str, Any]]:
        """The GTT orderbook; raises when it cannot be read."""
        return list(
            self._read_ok("gtt_orderbook", lambda: self.client.gtt_orderbook()).get("orderBook")
            or []
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
