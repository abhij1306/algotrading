"""The Fyers adapter behind `HatBroker` (HAT-DEC-BD2, HAT-BRK-081, -090, -091; 07 §9).

It wraps the tested `FyersBroker` (place, modify, cancel, the circuit breaker, the guarded
timeout, `SUBMIT_UNCONFIRMED`), `FyersClient` (is the SDK model usable) and `FyersAuthService`
(login link and code exchange), plus the GTT calls `FyersBroker` makes through the fyers SDK.
It never touches the SDK, a token file or an environment variable itself (HAT-OPS-051).

Every intent becomes one exact Fyers payload with product CNC; an invalid intent raises
`InvalidIntentError` before any broker call. Reads fail closed: when the broker cannot answer they
raise `BrokerUnavailableError`, never an empty list. Results carry fixed codes, not broker text.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

from app.brokers.base import BrokerUnavailableError as LegacyUnavailableError
from app.brokers.plugins.fyers import (
    BROKER_CIRCUIT_OPEN,
    CLIENT_NOT_INITIALIZED,
    SUBMIT_UNCONFIRMED,
    FyersBroker,
)
from app.services.fyers.auth_flow import FyersAuthError, FyersAuthService, get_fyers_auth
from app.services.fyers.token_store import TokenStoreError
from app.services.fyers_client import FyersClient, get_fyers_client
from app.services.symbol_master import symbol_master

from .broker import (
    CODE_INVALID_INTENT,
    CODE_REJECTED,
    CODE_UNAVAILABLE,
    CODE_UNCONFIRMED,
    AckResult,
    BrokerFunds,
    BrokerHolding,
    BrokerLoginError,
    BrokerOrder,
    BrokerPosition,
    BrokerTrade,
    BrokerUnavailableError,
    Capabilities,
    GttModify,
    GttRequest,
    GttResult,
    Instrument,
    InvalidIntentError,
    OrderIntent,
    OrderType,
    OrderUpdate,
    PlaceResult,
    SessionStatus,
    Side,
)
from .tags import TAG_LENGTH, is_hat_tag

__all__ = [
    "CODE_INVALID_RESPONSE",
    "FYERS_CAPABILITIES",
    "FyersHatBroker",
    "build_fyers_broker",
    "map_fyers_status",
    "map_session",
]

logger = logging.getLogger(__name__)

CODE_INVALID_RESPONSE = "broker_response_invalid"
CODE_OK = "ok"

# Fyers documents a 1-20 alphanumeric orderTag; GTT delivery is unverified on a live account
# (HAT-BRK-091, PLAN Q8): the owner flips `gtt_delivery` after a live verification.
FYERS_CAPABILITIES = Capabilities(
    gtt_delivery=False,
    tag_max_len=TAG_LENGTH,
    tag_alphabet="A-Za-z0-9",
    order_types=tuple(t.value for t in OrderType),
    products=("CNC",),
    static_ip_required=True,
)

# Fyers orderbook status codes: 1 cancelled, 2 traded, 4 transit, 5 rejected, 6 pending,
# 7 expired. Anything else (3 is unused, a new code, a malformed value) maps to `unconfirmed`,
# which blocks a retry until the order is looked up by tag.
_FYERS_STATUS = {1: "cancelled", 2: "filled", 4: "placed", 5: "rejected", 6: "placed", 7: "expired"}
_WORKING = {4, 6}


def _whole_number(value: object) -> int | None:
    """`value` as an int when it is an int, an integral float or a digit string; else None
    (never a truncated fraction, never a bool)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def map_fyers_status(code: object, filled_qty: object = 0, qty: object = 0) -> str:
    """A Fyers orderbook status as one of `hat_order.status`. Total: every input maps."""
    status = _whole_number(code)
    if status is None:
        return "unconfirmed"
    mapped = _FYERS_STATUS.get(status, "unconfirmed")
    if status in _WORKING:
        try:
            done, total = int(filled_qty), int(qty)  # type: ignore[call-overload]
        except (TypeError, ValueError, OverflowError):
            return mapped
        if total > 0 and done >= total:
            return "filled"
        if done > 0:
            return "partially_filled"
    return mapped


def map_session(*, connected: bool, has_client: bool, store_error: bool) -> SessionStatus:
    """The session status from the token store and the SDK client. Only LIVE can place orders."""
    if store_error:
        return SessionStatus.UNKNOWN
    if not connected:
        return SessionStatus.MISSING
    return SessionStatus.LIVE if has_client else SessionStatus.EXPIRED


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise InvalidOperation
    result = Decimal(str(value))
    if not result.is_finite():
        raise InvalidOperation
    return result


def _positive_price(value: object) -> Decimal:
    price = _decimal(value)
    if price <= 0:
        raise InvalidOperation
    return price


def _is_positive_qty(qty: object) -> bool:
    return isinstance(qty, int) and not isinstance(qty, bool) and qty >= 1


def _clean_tag(tag: object) -> str | None:
    """The tag of a broker row without Fyers' optional "<n>:" prefix, else None."""
    text = str(tag or "").split(":")[-1]
    return text or None


def _ack(reply: object) -> AckResult:
    if isinstance(reply, dict) and reply.get("s") == "ok":
        return AckResult(ok=True, code=CODE_OK)
    message = str(reply.get("message", "")) if isinstance(reply, dict) else ""
    if "timeout/unavailable" in message or CLIENT_NOT_INITIALIZED in message:
        return AckResult(ok=False, code=CODE_UNAVAILABLE)
    return AckResult(ok=False, code=CODE_REJECTED)


def _place_result(reply: object) -> tuple[str, str | None, str]:
    """(status, broker id, code) of an OrderResponse from FyersBroker."""
    if not isinstance(reply, dict):
        return "unconfirmed", None, CODE_UNCONFIRMED
    status = reply.get("status")
    order_id = str(reply.get("order_id") or "")
    if status == "SUBMITTED" and order_id:
        return "placed", order_id, CODE_OK
    if status == SUBMIT_UNCONFIRMED:
        return "unconfirmed", None, CODE_UNCONFIRMED
    message = str(reply.get("message") or "")
    if message == BROKER_CIRCUIT_OPEN or CLIENT_NOT_INITIALIZED in message:
        return "rejected", None, CODE_UNAVAILABLE
    return "rejected", None, CODE_REJECTED


class FyersHatBroker:
    """`HatBroker` over the existing Fyers client stack. Constructor arguments exist so tests
    inject `FakeBroker` (never a real SDK); production passes none."""

    def __init__(
        self,
        broker: FyersBroker | None = None,
        *,
        client: Callable[[], FyersClient] = get_fyers_client,
        auth: Callable[[], FyersAuthService] = get_fyers_auth,
        symbols: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self._broker = broker if broker is not None else FyersBroker()
        self._client = client
        self._auth = auth
        self._symbols = symbols if symbols is not None else symbol_master.known_symbols
        self._seen: dict[str, tuple[str, int]] = {}
        self._seen_lock = threading.Lock()

    # --- capabilities and session ---------------------------------------------------------

    def capabilities(self) -> Capabilities:
        return FYERS_CAPABILITIES

    def login_url(self) -> str:
        try:
            return self._auth().login_url()
        except Exception:
            logger.exception("Fyers login url failed")
            raise BrokerLoginError("login_unavailable") from None

    def complete_login(self, code: str) -> SessionStatus:
        try:
            self._auth().exchange(code)
        except TokenStoreError:
            logger.exception("Fyers token could not be stored")
            raise BrokerLoginError("token_store_failed") from None
        except FyersAuthError:
            logger.warning("Fyers refused the login code")
            raise BrokerLoginError("login_failed") from None
        except Exception:
            logger.exception("Fyers login failed")
            raise BrokerLoginError("login_failed") from None
        return self.session_status()

    def session_status(self) -> SessionStatus:
        try:
            connected = self._auth().status().connected
            has_client = self._client().fyers is not None
        except Exception:
            logger.exception("Fyers session status unreadable")
            return map_session(connected=False, has_client=False, store_error=True)
        return map_session(connected=connected, has_client=has_client, store_error=False)

    # --- writes ---------------------------------------------------------------------------

    @staticmethod
    def _order_payload(intent: OrderIntent) -> dict[str, Any]:
        """The order dict `FyersBroker.place_order` turns into the SDK payload. Raises
        InvalidIntentError for anything the adapter will not send."""
        try:
            if not _is_positive_qty(intent.qty):
                raise InvalidOperation
            side = Side(intent.side)
            order_type = OrderType(intent.order_type)
            if intent.product != "CNC":
                raise InvalidOperation
            if not is_hat_tag(intent.tag):
                raise InvalidOperation
            needs_limit = order_type in {OrderType.LIMIT, OrderType.SL}
            needs_trigger = order_type in {OrderType.SL, OrderType.SL_M}
            if needs_limit != (intent.price is not None) or needs_trigger != (
                intent.trigger_price is not None
            ):
                raise InvalidOperation
            order: dict[str, Any] = {
                "symbol": symbol_master.to_fyers(intent.symbol),
                "quantity": intent.qty,
                "side": side.value.upper(),
                "type": order_type.value,
                "product": "CNC",
                "order_tag": intent.tag,
            }
            if needs_limit:
                order["price"] = float(_positive_price(intent.price))
            if needs_trigger:
                order["trigger_price"] = float(_positive_price(intent.trigger_price))
        except (InvalidOperation, ValueError, TypeError):
            raise InvalidIntentError(CODE_INVALID_INTENT) from None
        return order

    def place_order(self, intent: OrderIntent) -> PlaceResult:
        order = self._order_payload(intent)
        try:
            reply = self._broker.place_order(order)
        except Exception:
            # The order may have reached the broker: never read this as a rejection.
            logger.exception("Fyers place_order raised")
            return PlaceResult("unconfirmed", None, CODE_UNCONFIRMED)
        status, order_id, code = _place_result(reply)
        return PlaceResult(status, order_id, code)

    def modify_order(
        self,
        broker_order_id: str,
        *,
        order_type: OrderType,
        qty: int | None = None,
        price: Decimal | None = None,
        trigger_price: Decimal | None = None,
    ) -> AckResult:
        try:
            params: dict[str, Any] = {"order_type": OrderType(order_type).value}
            if qty is not None:
                if not _is_positive_qty(qty):
                    raise InvalidOperation
                params["quantity"] = qty
            if price is not None:
                params["price"] = float(_positive_price(price))
            if trigger_price is not None:
                params["trigger_price"] = float(_positive_price(trigger_price))
        except (InvalidOperation, ValueError, TypeError):
            raise InvalidIntentError(CODE_INVALID_INTENT) from None
        return self._write(lambda: self._broker.modify_order(broker_order_id, params))

    def cancel_order(self, broker_order_id: str) -> AckResult:
        return self._write(lambda: self._broker.cancel_order(broker_order_id))

    @staticmethod
    def _write(call: Callable[[], object]) -> AckResult:
        try:
            return _ack(call())
        except Exception:
            logger.exception("Fyers write failed")
            return AckResult(ok=False, code=CODE_UNAVAILABLE)

    @staticmethod
    def _gtt_prices(trigger: Decimal, limit: Decimal) -> tuple[float, float]:
        trigger_price, limit_price = _positive_price(trigger), _positive_price(limit)
        if limit_price >= trigger_price:
            raise InvalidOperation  # a sell stop's limit sits below its trigger
        return float(trigger_price), float(limit_price)

    def place_gtt_stop(self, request: GttRequest) -> GttResult:
        try:
            if request.product != "CNC" or Side(request.side) is not Side.SELL:
                raise InvalidOperation
            if not _is_positive_qty(request.qty):
                raise InvalidOperation
            trigger, limit = self._gtt_prices(request.trigger_price, request.limit_price)
            gtt = {
                "symbol": symbol_master.to_fyers(request.symbol),
                "quantity": request.qty,
                "side": "SELL",
                "product": "CNC",
                "trigger_price": trigger,
                "price": limit,
            }
        except (InvalidOperation, ValueError, TypeError):
            raise InvalidIntentError(CODE_INVALID_INTENT) from None
        try:
            reply = self._broker.place_gtt(gtt)
        except Exception:
            logger.exception("Fyers place_gtt raised")
            return GttResult("unconfirmed", None, CODE_UNCONFIRMED)
        status, gtt_id, code = _place_result(reply)
        return GttResult(status, gtt_id, code)

    def modify_gtt(self, request: GttModify) -> AckResult:
        try:
            if not _is_positive_qty(request.qty) or not request.gtt_id:
                raise InvalidOperation
            trigger, limit = self._gtt_prices(request.trigger_price, request.limit_price)
        except (InvalidOperation, ValueError, TypeError):
            raise InvalidIntentError(CODE_INVALID_INTENT) from None
        params = {"quantity": request.qty, "trigger_price": trigger, "price": limit}
        return self._write(lambda: self._broker.modify_gtt(request.gtt_id, params))

    def cancel_gtt(self, gtt_id: str) -> AckResult:
        return self._write(lambda: self._broker.cancel_gtt(gtt_id))

    # --- reads (fail closed) --------------------------------------------------------------

    @staticmethod
    def _read(fetch: Callable[[], Any], parse: Callable[[Any], Any]) -> Any:  # noqa: ANN401
        """Fetch and parse; any failure is a fixed-code BrokerUnavailableError."""
        try:
            raw = fetch()
        except (LegacyUnavailableError, BrokerUnavailableError):
            logger.warning("Fyers read unavailable")
            raise BrokerUnavailableError(CODE_UNAVAILABLE) from None
        except Exception:
            logger.exception("Fyers read failed")
            raise BrokerUnavailableError(CODE_UNAVAILABLE) from None
        try:
            return parse(raw)
        except (KeyError, ValueError, TypeError, InvalidOperation, AttributeError):
            logger.exception("Fyers reply could not be read")
            raise BrokerUnavailableError(CODE_INVALID_RESPONSE) from None

    @staticmethod
    def _side(value: object) -> Side:
        return {1: Side.BUY, -1: Side.SELL}[int(value)]  # type: ignore[call-overload]

    def _order_row(self, row: dict[str, Any]) -> BrokerOrder:
        qty = int(row["qty"])
        filled = int(row.get("filledQty") or 0)
        return BrokerOrder(
            order_id=str(row["id"]),
            tag=_clean_tag(row.get("orderTag")),
            symbol=symbol_master.to_db(str(row["symbol"])),
            side=self._side(row["side"]),
            qty=qty,
            filled_qty=filled,
            status=map_fyers_status(row.get("status"), filled, qty),
            product=str(row.get("productType") or ""),
        )

    def orders(self) -> list[BrokerOrder]:
        return self._read(
            self._broker.fetch_orderbook, lambda rows: [self._order_row(r) for r in rows]
        )

    def trades(self) -> list[BrokerTrade]:
        def parse(rows: list[dict[str, Any]]) -> list[BrokerTrade]:
            return [
                BrokerTrade(
                    trade_id=str(r["tradeNumber"]),
                    order_id=str(r["orderNumber"]),
                    tag=_clean_tag(r.get("orderTag")),
                    symbol=symbol_master.to_db(str(r["symbol"])),
                    side=self._side(r["side"]),
                    qty=int(r["tradedQty"]),
                    price=_positive_price(r["tradePrice"]),
                    traded_at=None,
                )
                for r in rows
            ]

        return self._read(self._broker.fetch_tradebook, parse)

    def positions(self) -> list[BrokerPosition]:
        def parse(rows: list[dict[str, Any]]) -> list[BrokerPosition]:
            result = []
            for r in rows:
                qty = int(r["quantity"])
                result.append(
                    BrokerPosition(
                        symbol=symbol_master.to_db(str(r["symbol"])),
                        qty=qty if r["side"] == "LONG" else -qty,
                        avg_price=_decimal(r["entry_price"]),
                        product=str(r["product_type"]),
                    )
                )
            return result

        return self._read(self._broker.get_positions, parse)

    def holdings(self) -> list[BrokerHolding]:
        def parse(rows: list[dict[str, Any]]) -> list[BrokerHolding]:
            return [
                BrokerHolding(
                    symbol=symbol_master.to_db(str(r["symbol"])),
                    qty=int(r["quantity"]),
                    avg_price=_decimal(r.get("costPrice", 0)),
                )
                for r in rows
            ]

        return self._read(self._broker.fetch_holdings, parse)

    def funds(self) -> BrokerFunds:
        return self._read(
            self._broker.fetch_funds,
            lambda f: BrokerFunds(
                available=_decimal(f["available"]),
                used=_decimal(f["used"]),
                total=_decimal(f["total"]),
            ),
        )

    def instruments(self) -> list[Instrument]:
        def parse(symbols: Iterable[str]) -> list[Instrument]:
            return [Instrument(s, symbol_master.to_fyers(s)) for s in symbols]

        return self._read(self._symbols, parse)

    def order_updates(self) -> list[OrderUpdate]:
        """Orders whose status or filled quantity changed since the last successful poll."""
        current = self.orders()
        with self._seen_lock:
            updates = [
                OrderUpdate(o.order_id, o.tag, o.status, o.filled_qty)
                for o in current
                if self._seen.get(o.order_id) != (o.status, o.filled_qty)
            ]
            self._seen = {o.order_id: (o.status, o.filled_qty) for o in current}
        return updates


def build_fyers_broker() -> FyersHatBroker:
    """The production adapter: the process-wide Fyers broker, client and auth service."""
    return FyersHatBroker()
