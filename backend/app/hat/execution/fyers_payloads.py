"""Fyers payloads and replies as pure functions (architecture audit F8, X7).

Every intent becomes one exact Fyers payload with product CNC (`order_payload`, `gtt_payload` and
the modify fields), the one place a Decimal price becomes the float the SDK's JSON carries; an
invalid intent raises `InvalidIntentError` before any broker call. Every raw reply is parsed once,
its amounts through `money.require_exact` and its share counts as whole numbers
(`parse_positions`, `parse_funds`, `parse_holdings`, `parse_trade`, `parse_order_book`,
`parse_gtt_row`): a field HAT reads that is missing or not a number fails the read (one of
`UNREADABLE`), never reads as 0. Nothing here holds state or calls the SDK: `FyersHatBroker`
(`fyers_adapter.py`) does.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

from app.hat.data.symbols import symbol_master

from ..money import NotExactError, exact, require_exact
from .broker import (
    CODE_INVALID_INTENT,
    CODE_RATE_LIMITED,
    CODE_REJECTED,
    CODE_UNAVAILABLE,
    CODE_UNCONFIRMED,
    AckResult,
    BrokerFunds,
    BrokerGtt,
    BrokerHolding,
    BrokerOrder,
    BrokerPosition,
    BrokerTrade,
    GttModify,
    GttRequest,
    InvalidIntentError,
    OrderBook,
    OrderIntent,
    OrderType,
    Side,
)
from .fyers_broker import (
    BROKER_CIRCUIT_OPEN,
    CLIENT_NOT_INITIALIZED,
    SUBMIT_UNCONFIRMED,
    rate_limited,
)
from .tags import is_gtt_tag, is_hat_tag

__all__ = [
    "CODE_OK",
    "FYERS_ORDER_TYPES",
    "UNREADABLE",
    "ack_result",
    "gtt_modify_fields",
    "gtt_payload",
    "map_fyers_status",
    "order_modify_fields",
    "order_payload",
    "parse_funds",
    "parse_gtt_row",
    "parse_holdings",
    "parse_order_book",
    "parse_order_row",
    "parse_positions",
    "parse_trade",
    "place_result",
]

logger = logging.getLogger(__name__)

FYERS_ORDER_TYPES = {OrderType.LIMIT: 1, OrderType.MARKET: 2, OrderType.SL_M: 3, OrderType.SL: 4}
# The Fyers order "type" code back to HAT's order type (`FYERS_ORDER_TYPES` reversed).
_ORDER_TYPE_OF_CODE = {code: order_type for order_type, code in FYERS_ORDER_TYPES.items()}
_LIMIT_PRICED = frozenset({OrderType.LIMIT, OrderType.SL})
_STOP_PRICED = frozenset({OrderType.SL, OrderType.SL_M})
_SIDE_CODE = {Side.BUY: 1, Side.SELL: -1}


def _order_type(code: object) -> OrderType | None:
    """The order type a book row's `type` names; None for a code this adapter does not know."""
    try:
        return _ORDER_TYPE_OF_CODE.get(int(code))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


def _book_price(value: object) -> Decimal | None:
    """A book row's limit price; None when it has none (0, missing or not a number)."""
    return exact(value, floats=True, positive=True)


CODE_OK = "ok"


# Fyers orderbook status codes: 1 cancelled, 2 traded, 4 transit, 5 rejected, 6 pending,
# 7 expired. Anything else (3 is unused, a new code, a malformed value) maps to `unconfirmed`,
# which blocks a retry until the order is looked up by tag.
_FYERS_STATUS = {1: "cancelled", 2: "filled", 4: "placed", 5: "rejected", 6: "placed", 7: "expired"}
_WORKING = {4, 6}
# A GTT book row's `ord_status` uses the same codes: 4 and 6 still rest, 2 triggered (its sale
# is the fill sync's to attribute), 1, 5 and 7 ended without selling.
_GTT_ENDED = {1, 5, 7}
_GTT_KNOWN = {2, 4, 6}
# what a reply that cannot be read raises while it is parsed
UNREADABLE = (KeyError, ValueError, TypeError, InvalidOperation, AttributeError, OverflowError)
_GTT_OCO = 2  # `gtt_oco_ind`: 1 a single GTT, 2 an OCO
_INTEGER_TEXT = re.compile(r"-?\d+", re.ASCII)  # ASCII: "¹" is a str digit, not an int


def _whole_number(value: object) -> int | None:
    """`value` as an int when it is an int, an integral float or an ASCII integer string
    (`-?[0-9]+`, surrounding spaces ignored); else None (never a truncated fraction, never a bool,
    never a Unicode digit `int` refuses such as "¹")."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str) and _INTEGER_TEXT.fullmatch(value.strip()):
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


def _intent_price(value: object) -> Decimal:
    """An order's price as HAT sends it: an exact positive Decimal; a float, text or anything
    else is refused (rule 8)."""
    if not isinstance(value, Decimal):
        raise NotExactError("price: not a Decimal")
    return require_exact(value, "price", positive=True)


def _is_positive_qty(qty: object) -> bool:
    return isinstance(qty, int) and not isinstance(qty, bool) and qty >= 1


def _sdk_price(value: object) -> float:
    """The one place an order price leaves Decimal: the float the SDK's JSON carries, of an exact
    positive Decimal (`_intent_price` refuses anything else)."""
    return float(_intent_price(value))


# --- payloads: one intent, one exact SDK dict ---------------------------------------------------


def order_payload(intent: OrderIntent, fyers_symbol: Callable[[str], str]) -> dict[str, Any]:
    """The Fyers `place_order` payload of `intent`, under the Fyers symbol `fyers_symbol` names:
    product CNC, DAY validity, the HT tag as `orderTag`, a limit price only for LIMIT and SL and a
    trigger only for SL and SL-M (0 otherwise). Raises InvalidIntentError for anything HAT will
    not send."""
    try:
        if not _is_positive_qty(intent.qty) or intent.product != "CNC":
            raise InvalidOperation
        if not is_hat_tag(intent.tag):
            raise InvalidOperation
        side, order_type = Side(intent.side), OrderType(intent.order_type)
        needs_limit, needs_trigger = order_type in _LIMIT_PRICED, order_type in _STOP_PRICED
        if needs_limit != (intent.price is not None) or needs_trigger != (
            intent.trigger_price is not None
        ):
            raise InvalidOperation
        return {
            "symbol": fyers_symbol(intent.symbol),
            "qty": intent.qty,
            "type": FYERS_ORDER_TYPES[order_type],
            "side": _SIDE_CODE[side],
            "productType": "CNC",
            "limitPrice": _sdk_price(intent.price) if needs_limit else 0,
            "stopPrice": _sdk_price(intent.trigger_price) if needs_trigger else 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "orderTag": intent.tag,
        }
    except (InvalidOperation, ValueError, TypeError):
        raise InvalidIntentError(CODE_INVALID_INTENT) from None


def order_modify_fields(
    order_type: object, qty: object, price: object, trigger_price: object
) -> dict[str, Any] | None:
    """The Fyers `modify_order` fields (besides the order id) that describe the order after the
    change; its type is required, so the Fyers type is never guessed. None when the type needs a
    price that is not given (refused, nothing is sent). Raises InvalidIntentError for a type,
    quantity or price HAT will not send."""
    try:
        kind = OrderType(order_type)  # type: ignore[arg-type]
        if qty is not None and not _is_positive_qty(qty):
            raise InvalidOperation
        limit = None if price is None else _sdk_price(price)
        trigger = None if trigger_price is None else _sdk_price(trigger_price)
    except (InvalidOperation, ValueError, TypeError):
        raise InvalidIntentError(CODE_INVALID_INTENT) from None
    if (kind in _LIMIT_PRICED and limit is None) or (kind in _STOP_PRICED and trigger is None):
        return None
    fields: dict[str, Any] = {
        "type": FYERS_ORDER_TYPES[kind],
        "limitPrice": limit if kind in _LIMIT_PRICED else 0,
        "stopPrice": trigger if kind in _STOP_PRICED else 0,
    }
    if qty is not None:
        fields["qty"] = qty
    return fields


def _gtt_leg(qty: object, trigger: object, limit: object) -> dict[str, Any]:
    """The single leg of a sell stop GTT: a positive quantity, and a limit below its trigger."""
    trigger_price, limit_price = _intent_price(trigger), _intent_price(limit)
    if not _is_positive_qty(qty) or limit_price >= trigger_price:
        raise InvalidOperation  # a sell stop's limit sits below its trigger
    return {"price": float(limit_price), "triggerPrice": float(trigger_price), "qty": qty}


def gtt_payload(request: GttRequest, fyers_symbol: Callable[[str], str]) -> dict[str, Any]:
    """The Fyers GTT place payload of a single-leg CNC sell stop, its stop_gtt tag as
    `orderTag` (Fyers takes a tag on a GTT, recorded 2026-10-22). Raises InvalidIntentError for
    anything HAT will not send."""
    try:
        if request.product != "CNC" or Side(request.side) is not Side.SELL:
            raise InvalidOperation
        if not is_gtt_tag(request.tag):
            raise InvalidOperation  # every GTT HAT places carries its stop_gtt tag
        leg = _gtt_leg(request.qty, request.trigger_price, request.limit_price)
        return {
            "side": _SIDE_CODE[Side.SELL],
            "symbol": fyers_symbol(request.symbol),
            "productType": "CNC",
            "orderInfo": {"leg1": leg},
            "orderTag": request.tag,
        }
    except (InvalidOperation, ValueError, TypeError):
        raise InvalidIntentError(CODE_INVALID_INTENT) from None


def gtt_modify_fields(request: GttModify) -> dict[str, Any]:
    """The Fyers GTT modify fields (besides the GTT id): the leg after the change."""
    try:
        if not request.gtt_id:
            raise InvalidOperation
        leg = _gtt_leg(request.qty, request.trigger_price, request.limit_price)
    except (InvalidOperation, ValueError, TypeError):
        raise InvalidIntentError(CODE_INVALID_INTENT) from None
    return {"orderInfo": {"leg1": leg}}


# --- replies: each raw Fyers reply parsed once ---------------------------------------------------


def _count(value: object, name: str, minimum: int | None = 0) -> int:
    """A share count from a reply: a whole number of at least `minimum` (None: any sign, as a
    net position's), never a truncated fraction."""
    whole = _whole_number(value)
    if whole is None or (minimum is not None and whole < minimum):
        raise ValueError(f"{name}: not a count")
    return whole


def _id(value: object, name: str) -> str:
    """A broker id from a reply: text or a whole number, never missing (a null id read as "None"
    would make every such row the same order, trade or GTT)."""
    if isinstance(value, bool) or not isinstance(value, str | int) or str(value).strip() == "":
        raise ValueError(f"{name}: missing")
    return str(value)


def _text(value: object, name: str) -> str:
    """A reply's text field, present and not empty (never a default)."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name}: missing")
    return value


def parse_positions(rows: Iterable[dict[str, Any]]) -> list[BrokerPosition]:
    """The open positions of a `netPositions` list (a net quantity of 0 is none): the signed net
    quantity, the average of the side held (`buyAvg` long, `sellAvg` short) and the product. A
    missing or unreadable quantity, average or product raises (never an average of 0, never an
    INTRADAY product)."""
    positions = []
    for row in rows:
        qty = _count(row["netQty"], "netQty", minimum=None)
        if qty == 0:
            continue
        average = "buyAvg" if qty > 0 else "sellAvg"
        positions.append(
            BrokerPosition(
                symbol=symbol_master.to_db(str(row["symbol"])),
                qty=qty,
                avg_price=require_exact(row[average], average, floats=True, positive=True),
                product=_text(row["productType"], "productType"),
            )
        )
    return positions


def parse_holdings(rows: Iterable[dict[str, Any]]) -> list[BrokerHolding]:
    """The demat holdings of a `holdings` list. A missing or unreadable quantity or `costPrice`
    raises (a bonus share may cost 0 [A]; a negative cost is not a cost)."""
    holdings = []
    for row in rows:
        cost = require_exact(row["costPrice"], "costPrice", floats=True)
        if cost < 0:
            raise ValueError("costPrice: negative")
        holdings.append(
            BrokerHolding(
                symbol=symbol_master.to_db(str(row["symbol"])),
                qty=_count(row["quantity"], "quantity"),
                avg_price=cost,
            )
        )
    return holdings


# The `fund_limit` rows HAT reads, by title (Fyers documents ten; the rest are never parsed, so
# an odd collateral amount cannot fail the read).
_FUND_ROWS = {"Available Balance": "available", "Utilized Amount": "used", "Total Balance": "total"}


def parse_funds(reply: dict[str, Any]) -> BrokerFunds:
    """The funds of an ok `funds` reply, from its `fund_limit` rows: "Available Balance" and
    "Utilized Amount" must be there with an amount, and a "Total Balance" Fyers leaves out is
    their exact sum. A reply without them raises: it is not a reply of 0 cash."""
    rows = reply["fund_limit"]
    if not isinstance(rows, list):
        raise TypeError("fund_limit: not a list")
    amounts: dict[str, Decimal] = {}
    for row in rows:
        field = _FUND_ROWS.get(row.get("title")) if isinstance(row, dict) else None
        if field is not None:
            amounts[field] = require_exact(row["equityAmount"], row["title"], floats=True)
    available, used = amounts["available"], amounts["used"]
    return BrokerFunds(available=available, used=used, total=amounts.get("total", available + used))


def parse_trade(row: dict[str, Any]) -> BrokerTrade:
    """One `tradeBook` row: its traded quantity a whole number, its price exact and positive."""
    tag, gtt_tag = _row_tags(row.get("orderTag"))
    return BrokerTrade(
        trade_id=_id(row["tradeNumber"], "tradeNumber"),
        order_id=_id(row["orderNumber"], "orderNumber"),
        tag=tag,
        symbol=symbol_master.to_db(str(row["symbol"])),
        side=_side(row["side"]),
        qty=_count(row["tradedQty"], "tradedQty", minimum=1),
        price=require_exact(row["tradePrice"], "tradePrice", floats=True, positive=True),
        traded_at=None,
        gtt_tag=gtt_tag,
    )


def _side(value: object) -> Side:
    return {1: Side.BUY, -1: Side.SELL}[_count(value, "side", minimum=None)]


def _clean_tag(tag: object) -> str | None:
    """The tag of a broker row without Fyers' optional "<n>:" prefix, else None."""
    text = str(tag or "").split(":")[-1]
    return text or None


# Fyers shows a GTT's tag on its book row as "<n>:GTT<tag>" and an untagged GTT's as
# "<n>:GTTUntagged" (recorded 2026-10-22).
_GTT_TAG_PREFIX = "GTT"
_GTT_UNTAGGED = "GTTUntagged"


def _gtt_tag(tag: object) -> str | None:
    """The HT tag of one of HAT's GTTs in a broker row's tag (Fyers' "<n>:" prefix and the "GTT"
    it puts before a GTT's tag stripped); None for no tag, "GTTUntagged", a foreign tag or a HAT
    tag of another purpose."""
    text = _clean_tag(tag)
    if text is not None and text.startswith(_GTT_TAG_PREFIX):
        text = text[len(_GTT_TAG_PREFIX) :]
    return text if is_gtt_tag(text) else None


def _row_tags(tag: object) -> tuple[str | None, str | None]:
    """(tag, gtt_tag) of an order or trade row. A row under one of HAT's GTT tags (bare, or as
    Fyers shows a GTT's) is the sale that GTT triggered into [verify on a live account: whether
    and how the child carries the tag], not an order HAT placed: its tag is None and the GTT's
    is `gtt_tag`. Fyers' "GTTUntagged" is no tag."""
    gtt_tag = _gtt_tag(tag)
    if gtt_tag is not None:
        return None, gtt_tag
    text = _clean_tag(tag)
    # Fyers' mark of an untagged GTT is no tag: such a sale is attributed by its shape
    return (None if text == _GTT_UNTAGGED else text), None


def ack_result(reply: object) -> AckResult:
    """The `AckResult` of a modify or cancel reply from FyersBroker."""
    if isinstance(reply, dict) and reply.get("s") == "ok":
        return AckResult(ok=True, code=CODE_OK)
    if rate_limited(reply):
        return AckResult(ok=False, code=CODE_RATE_LIMITED)
    message = str(reply.get("message", "")) if isinstance(reply, dict) else ""
    if "timeout/unavailable" in message or CLIENT_NOT_INITIALIZED in message:
        return AckResult(ok=False, code=CODE_UNAVAILABLE)
    return AckResult(ok=False, code=CODE_REJECTED)


def place_result(reply: object) -> tuple[str, str | None, str]:
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
    if rate_limited(reply.get("details")):
        return "rejected", None, CODE_RATE_LIMITED  # refused: nothing was placed
    return "rejected", None, CODE_REJECTED


def parse_gtt_row(row: dict[str, Any]) -> BrokerGtt | None:
    """One GTT book row, or None for a row that cannot be one of HAT's stops: an OCO
    (`gtt_oco_ind` 2 or a second leg), a product other than CNC, or a GTT that ended without
    selling (`ord_status` 1 cancelled, 5 rejected, 7 expired). A GTT resting (4 transit,
    6 pending, or no status) or triggered (2) is kept: a lost answer whose GTT has since
    triggered is adopted, so the fill sync attributes its sale. The book's keys are the ones
    recorded live on 2026-10-22: `tran_side`, `product_type`, `ordertag` ("<n>:GTT<tag>",
    read as the HT tag of one of HAT's GTTs, else None and `foreign` unless untagged) and the
    flat `price_trigger`/`qty`; the shape a GTT is placed with is read too (`side`,
    `productType`, the single leg in `orderInfo.leg1` / `leg1`) [verify on a live account].
    A single-leg CNC row with a status code it does not know, or whose side, trigger or
    quantity cannot be read, raises, so the read fails instead of missing a GTT."""
    info = row.get("orderInfo") or {}
    oco = _whole_number(row.get("gtt_oco_ind")) == _GTT_OCO
    if oco or row.get("leg2") or info.get("leg2"):
        return None
    product = row.get("product_type", row.get("productType"))
    if product is not None and product != "CNC":
        return None
    status = row.get("ord_status", row.get("status"))
    if status is not None:
        code = _whole_number(status)
        if code in _GTT_ENDED:
            return None
        if code not in _GTT_KNOWN:
            raise ValueError("gtt status")
    leg = info.get("leg1") or row.get("leg1")
    trigger, qty = (leg["triggerPrice"], leg["qty"]) if leg else (row["price_trigger"], row["qty"])
    whole = _whole_number(qty)
    if whole is None:
        raise ValueError("gtt qty")
    tag = _gtt_tag(row.get("ordertag"))
    shown = _clean_tag(row.get("ordertag"))
    return BrokerGtt(
        gtt_id=_id(row["id"], "id"),
        symbol=symbol_master.to_db(str(row["symbol"])),
        side=_side(row["tran_side"] if "tran_side" in row else row["side"]),
        qty=whole,
        trigger_price=require_exact(trigger, "trigger", floats=True, positive=True),
        tag=tag,
        foreign=tag is None and shown not in (None, _GTT_UNTAGGED),
    )


def parse_order_row(row: dict[str, Any]) -> BrokerOrder:
    qty = _count(row["qty"], "qty")
    filled = _count(row.get("filledQty") or 0, "filledQty")
    tag, gtt_tag = _row_tags(row.get("orderTag"))
    return BrokerOrder(
        order_id=_id(row["id"], "id"),
        tag=tag,
        symbol=symbol_master.to_db(str(row["symbol"])),
        side=_side(row["side"]),
        qty=qty,
        filled_qty=filled,
        status=map_fyers_status(row.get("status"), filled, qty),
        product=str(row.get("productType") or ""),
        order_type=_order_type(row.get("type")),
        price=_book_price(row.get("limitPrice")),
        gtt_tag=gtt_tag,
    )


def parse_order_book(rows: Iterable[Any]) -> OrderBook:
    """Every row of the book. A row without an HT tag that cannot be read is skipped and
    counted (a manual or foreign order must not fail the whole read, D118); an HT-tagged row
    (one of HAT's GTT sales included) that cannot be read raises, so the read fails instead
    of missing one of HAT's orders."""
    book = OrderBook()
    for row in rows:
        try:
            book.append(parse_order_row(row))
        except UNREADABLE:
            tag = row.get("orderTag") if isinstance(row, dict) else None
            own, gtt_tag = _row_tags(tag)
            if is_hat_tag(own) or gtt_tag is not None:
                raise
            book.skipped += 1
    return book
