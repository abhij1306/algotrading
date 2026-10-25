"""Kite (Zerodha) payloads and replies as pure functions, as `fyers_payloads` is for Fyers.

Every intent becomes the exact keyword arguments of one `KiteConnect` call (`order_params`,
`order_modify_params`, `gtt_params`): exchange NSE, variety regular, product CNC, DAY validity,
the HT tag as `tag` (Kite takes 20 alphanumeric characters, an HT tag's length), a Decimal price
as the float the SDK sends; an invalid intent raises `InvalidIntentError` before any call. A
MARKET or SL-M order carries `market_protection=-1` (Kite's automatic protection, which Kite
requires on an API market order) [verify on a live account].

Every raw reply is parsed once, its amounts through `money.require_exact` and its share counts
as whole numbers: a field HAT reads that is missing or not a number fails the read (one of
`UNREADABLE`), never reads as 0. Kite names a stock by its trading symbol (`SBIN`, `XYZ-BE` for a
trade-for-trade one); each parser takes `to_db`, the trading symbol's database symbol, which the
adapter reads from `hat_broker_symbol` (`kite_db_symbol` without a stored row).

Two shapes differ from Fyers': a Kite GTT carries no tag, so every GTT in the book is untagged
(HAT tells its own by symbol, side, quantity and trigger, never adopting another), and a Kite
trade carries no tag, so a trade takes its order's (`parse_trades`; a trade whose order is not in
the day's book fails the read: it could be one of HAT's). The shapes are Kite Connect v3's
published ones (`tests/hat/fixtures/kite_replies.json`) [verify on a live account].
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from ..money import exact, require_exact
from .broker import (
    CODE_INVALID_INTENT,
    CODE_OK,
    CODE_RATE_LIMITED,
    CODE_REJECTED,
    CODE_SESSION_EXPIRED,
    CODE_UNAVAILABLE,
    CODE_UNCONFIRMED,
    AckResult,
    BrokerFunds,
    BrokerGtt,
    BrokerHolding,
    BrokerOrder,
    BrokerPosition,
    BrokerTrade,
    GttRequest,
    InvalidIntentError,
    OrderBook,
    OrderIntent,
    OrderType,
    Side,
    TradeBook,
)
from .fyers_payloads import (
    UNREADABLE,
    intent_price,
    is_positive_qty,
    reply_count,
    reply_id,
    whole_number,
)
from .kite_broker import EXPIRED, NOT_SENT, OK, RATE_LIMITED, REFUSED, KiteReply
from .tags import is_gtt_tag, is_hat_tag

__all__ = [
    "EXCHANGE",
    "KITE_ORDER_TYPES",
    "UNREADABLE",
    "ack_result",
    "check_gtt_leg",
    "check_gtt_request",
    "gtt_modify_params",
    "gtt_params",
    "kite_db_symbol",
    "ltp_key",
    "map_kite_status",
    "order_modify_params",
    "order_params",
    "parse_funds",
    "parse_gtt_row",
    "parse_holdings",
    "parse_ltp",
    "parse_order_book",
    "parse_order_row",
    "parse_positions",
    "parse_trades",
    "place_result",
    "rows_of",
    "stop_is_below",
]

EXCHANGE = "NSE"
VARIETY = "regular"
KITE_ORDER_TYPES = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.SL: "SL",
    OrderType.SL_M: "SL-M",
}
_ORDER_TYPE_OF = {name: order_type for order_type, name in KITE_ORDER_TYPES.items()}
_LIMIT_PRICED = frozenset({OrderType.LIMIT, OrderType.SL})
_STOP_PRICED = frozenset({OrderType.SL, OrderType.SL_M})
_PROTECTED = frozenset({OrderType.MARKET, OrderType.SL_M})  # Kite's market_protection applies
AUTO_PROTECTION = -1
_SIDE = {Side.BUY: "BUY", Side.SELL: "SELL"}
_SIDE_OF = {name: side for side, name in _SIDE.items()}
_BE_SUFFIX = "-BE"

# Kite's order statuses. Every other one ("PUT ORDER REQ RECEIVED", "VALIDATION PENDING",
# "OPEN PENDING", "MODIFY PENDING", "TRIGGER PENDING", "CANCEL PENDING", "AMO REQ RECEIVED", ...)
# is an order still working at Kite; a status Kite does not document maps to `unconfirmed`, which
# blocks a retry until the order is looked up by tag.
_ENDED = {"COMPLETE": "filled", "CANCELLED": "cancelled", "REJECTED": "rejected"}
_WORKING = frozenset(
    {
        "OPEN",
        "TRIGGER PENDING",
        "PUT ORDER REQ RECEIVED",
        "VALIDATION PENDING",
        "OPEN PENDING",
        "MODIFY VALIDATION PENDING",
        "MODIFY PENDING",
        "MODIFIED",
        "CANCEL PENDING",
        "AMO REQ RECEIVED",
    }
)
# A GTT's statuses: `active` rests, `triggered` sold (its sale is the fill sync's to attribute);
# the rest ended without selling
_GTT_KNOWN = frozenset({"active", "triggered"})
_GTT_ENDED = frozenset({"cancelled", "rejected", "deleted", "expired", "disabled"})
_GTT_SINGLE = "single"


def kite_db_symbol(tradingsymbol: str) -> str:
    """The database symbol of a Kite NSE equity trading symbol without a stored mapping: the
    symbol itself, a trade-for-trade one without its `-BE` (a name such as `BAJAJ-AUTO` keeps
    its hyphen)."""
    return tradingsymbol.removesuffix(_BE_SUFFIX)


def _sdk_price(value: object) -> float:
    """The one place a price leaves Decimal: the float the SDK sends, of an exact positive
    Decimal (`intent_price` refuses anything else)."""
    return float(intent_price(value))


# --- payloads: one intent, one exact set of SDK keyword arguments ------------------------------


def order_params(intent: OrderIntent, tradingsymbol: Callable[[str], str]) -> dict[str, Any]:
    """The `KiteConnect.place_order` keyword arguments of `intent`, under the trading symbol
    `tradingsymbol` names: a limit price only for LIMIT and SL, a trigger only for SL and SL-M,
    automatic market protection on MARKET and SL-M. Raises InvalidIntentError for anything HAT
    will not send."""
    try:
        if not is_positive_qty(intent.qty) or intent.product != "CNC":
            raise InvalidOperation
        if not is_hat_tag(intent.tag):
            raise InvalidOperation
        side, order_type = Side(intent.side), OrderType(intent.order_type)
        needs_limit, needs_trigger = order_type in _LIMIT_PRICED, order_type in _STOP_PRICED
        if needs_limit != (intent.price is not None) or needs_trigger != (
            intent.trigger_price is not None
        ):
            raise InvalidOperation
        params: dict[str, Any] = {
            "variety": VARIETY,
            "exchange": EXCHANGE,
            "tradingsymbol": tradingsymbol(intent.symbol),
            "transaction_type": _SIDE[side],
            "quantity": intent.qty,
            "product": "CNC",
            "order_type": KITE_ORDER_TYPES[order_type],
            "validity": "DAY",
            "tag": intent.tag,
        }
        if needs_limit:
            params["price"] = _sdk_price(intent.price)
        if needs_trigger:
            params["trigger_price"] = _sdk_price(intent.trigger_price)
        if order_type in _PROTECTED:
            params["market_protection"] = AUTO_PROTECTION
        return params
    except (InvalidOperation, ValueError, TypeError):
        raise InvalidIntentError(CODE_INVALID_INTENT) from None


def order_modify_params(
    order_type: object, qty: object, price: object, trigger_price: object
) -> dict[str, Any] | None:
    """The `KiteConnect.modify_order` keyword arguments (besides the variety and the order id)
    that describe the order after the change; None when the type needs a price that is not given
    (refused, nothing is sent). Raises InvalidIntentError for a type, quantity or price HAT will
    not send."""
    try:
        kind = OrderType(order_type)  # type: ignore[arg-type]
        if qty is not None and not is_positive_qty(qty):
            raise InvalidOperation
        limit = None if price is None else _sdk_price(price)
        trigger = None if trigger_price is None else _sdk_price(trigger_price)
    except (InvalidOperation, ValueError, TypeError):
        raise InvalidIntentError(CODE_INVALID_INTENT) from None
    if (kind in _LIMIT_PRICED and limit is None) or (kind in _STOP_PRICED and trigger is None):
        return None
    params: dict[str, Any] = {"order_type": KITE_ORDER_TYPES[kind]}
    if qty is not None:
        params["quantity"] = qty
    if kind in _LIMIT_PRICED:
        params["price"] = limit
    if kind in _STOP_PRICED:
        params["trigger_price"] = trigger
    if kind in _PROTECTED:
        params["market_protection"] = AUTO_PROTECTION
    return params


def stop_is_below(trigger: Decimal, last_price: Decimal) -> bool:
    """Whether a sell stop's trigger sits below the last price. Kite reads a single GTT's
    direction from the last price it is placed with, so a trigger at or above it would be a
    trigger on a rise: never sent."""
    return trigger < last_price


def _leg_prices(qty: object, trigger: object, limit: object) -> tuple[Decimal, Decimal]:
    """(trigger, limit) of a sell stop's leg: a positive quantity, and a limit below its
    trigger; raises otherwise."""
    trigger_price, limit_price = intent_price(trigger), intent_price(limit)
    if not is_positive_qty(qty) or limit_price >= trigger_price:
        raise InvalidOperation  # a sell stop's limit sits below its trigger
    return trigger_price, limit_price


def check_gtt_leg(qty: object, trigger: object, limit: object) -> Decimal:
    """The trigger of a GTT leg HAT will send; InvalidIntentError otherwise. The adapter checks a
    GTT with it before it reads the last price, so an invalid one makes no call at all."""
    try:
        return _leg_prices(qty, trigger, limit)[0]
    except (InvalidOperation, ValueError, TypeError):
        raise InvalidIntentError(CODE_INVALID_INTENT) from None


def check_gtt_request(request: GttRequest) -> Decimal:
    """The trigger of a single-leg CNC sell stop HAT will place, its stop_gtt tag checked
    (Kite takes no tag on a GTT, so it is not sent); InvalidIntentError otherwise."""
    try:
        if request.product != "CNC" or Side(request.side) is not Side.SELL:
            raise InvalidOperation
        if not is_gtt_tag(request.tag):
            raise InvalidOperation  # every GTT HAT places has its stop_gtt tag
    except (InvalidOperation, ValueError, TypeError):
        raise InvalidIntentError(CODE_INVALID_INTENT) from None
    return check_gtt_leg(request.qty, request.trigger_price, request.limit_price)


def _gtt_kwargs(
    tradingsymbol: str, qty: object, trigger: object, limit: object, last_price: object
) -> dict[str, Any]:
    try:
        if not tradingsymbol:
            raise InvalidOperation
        trigger_price, limit_price = _leg_prices(qty, trigger, limit)
        last = intent_price(last_price)
        if not stop_is_below(trigger_price, last):
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError):
        raise InvalidIntentError(CODE_INVALID_INTENT) from None
    return {
        "trigger_type": _GTT_SINGLE,
        "tradingsymbol": tradingsymbol,
        "exchange": EXCHANGE,
        "trigger_values": [float(trigger_price)],
        "last_price": float(last),
        "orders": [
            {
                "transaction_type": "SELL",
                "quantity": qty,
                "order_type": "LIMIT",
                "product": "CNC",
                "price": float(limit_price),
            }
        ],
    }


def gtt_params(
    request: GttRequest, tradingsymbol: Callable[[str], str], last_price: Decimal
) -> dict[str, Any]:
    """The `KiteConnect.place_gtt` keyword arguments of a single-leg CNC sell stop placed against
    `last_price` (`check_gtt_request`). Raises InvalidIntentError for anything HAT will not send,
    a trigger not below the last price included (`stop_is_below`: the adapter refuses such a
    stop before it gets here)."""
    check_gtt_request(request)
    return _gtt_kwargs(
        tradingsymbol(request.symbol),
        request.qty,
        request.trigger_price,
        request.limit_price,
        last_price,
    )


def gtt_modify_params(
    tradingsymbol: str, qty: object, trigger: object, limit: object, last_price: Decimal
) -> dict[str, Any]:
    """The `KiteConnect.modify_gtt` keyword arguments (besides the trigger id): the whole GTT
    after the change, as Kite replaces it. Raises InvalidIntentError as `gtt_params` does."""
    return _gtt_kwargs(tradingsymbol, qty, trigger, limit, last_price)


def ltp_key(tradingsymbol: str) -> str:
    """The `KiteConnect.ltp` key of an NSE trading symbol."""
    return f"{EXCHANGE}:{tradingsymbol}"


def parse_ltp(reply: Mapping[str, Any], key: str) -> Decimal:
    """The last price of `key` in an `ltp` reply: exact and positive, or the read fails."""
    return require_exact(reply[key]["last_price"], "last_price", floats=True, positive=True)


# --- replies: each raw Kite reply parsed once --------------------------------------------------


def rows_of(reply: object) -> list[Any]:
    """A list reply's rows; TypeError for anything else (an ok answer without its list is an
    unreadable book, never an empty one: a tag lookup that read it as empty would send the same
    order again)."""
    if not isinstance(reply, list):
        raise TypeError("reply: not a list")
    return reply


def _side(value: object) -> Side:
    return _SIDE_OF[str(value).upper()]


def _symbol(row: Mapping[str, Any], to_db: Callable[[str], str]) -> str:
    text = row["tradingsymbol"]
    if not isinstance(text, str) or not text:
        raise ValueError("tradingsymbol: missing")
    return to_db(text)


def map_kite_status(status: object, filled_qty: object = 0, qty: object = 0) -> str:
    """A Kite order status as one of `hat_order.status`. Total: every input maps."""
    text = str(status or "").strip().upper()
    if text in _ENDED:
        return _ENDED[text]
    if text not in _WORKING:
        return "unconfirmed"
    done, total = whole_number(filled_qty), whole_number(qty)
    if done is None or total is None:
        return "placed"
    if total > 0 and done >= total:
        return "filled"
    return "partially_filled" if done > 0 else "placed"


def _row_tags(tag: object) -> tuple[str | None, str | None]:
    """(tag, gtt_tag) of an order row: a row under one of HAT's GTT tags is that GTT's sale (Kite
    tags no GTT, so only an order HAT placed under such a tag would show one); any other text is
    the order's own tag, an empty one none."""
    text = tag if isinstance(tag, str) and tag else None
    if is_gtt_tag(text):
        return None, text
    return text, None


def parse_order_row(row: Mapping[str, Any], to_db: Callable[[str], str]) -> BrokerOrder:
    qty = reply_count(row["quantity"], "quantity")
    filled = reply_count(row.get("filled_quantity") or 0, "filled_quantity")
    tag, gtt_tag = _row_tags(row.get("tag"))
    price = row.get("price")
    return BrokerOrder(
        order_id=reply_id(row["order_id"], "order_id"),
        tag=tag,
        symbol=_symbol(row, to_db),
        side=_side(row["transaction_type"]),
        qty=qty,
        filled_qty=filled,
        status=map_kite_status(row.get("status"), filled, qty),
        product=str(row.get("product") or ""),
        order_type=_ORDER_TYPE_OF.get(str(row.get("order_type") or "")),
        price=require_exact(price, "price", floats=True, positive=True) if price else None,
        gtt_tag=gtt_tag,
    )


def parse_order_book(rows: Iterable[Any], to_db: Callable[[str], str]) -> OrderBook:
    """Every row of the day's orders. A row without an HT tag that cannot be read is skipped and
    counted (a manual or foreign order must not fail the whole read, D118); an HT-tagged row that
    cannot be read raises, so the read fails instead of missing one of HAT's orders."""
    book = OrderBook()
    for row in rows_of(rows):
        try:
            book.append(parse_order_row(row, to_db))
        except UNREADABLE:
            tag = row.get("tag") if isinstance(row, Mapping) else None
            if is_hat_tag(tag):
                raise
            book.skipped += 1
    return book


def parse_trades(
    rows: Iterable[Mapping[str, Any]], orders: Iterable[Any], to_db: Callable[[str], str]
) -> TradeBook:
    """The day's trades, each with its order's tags from the day's order rows (`orders`, raw):
    a Kite trade row has none of its own. A trade whose order is not among them raises (it may be
    one of HAT's, and a fill must never read as foreign). A trade of an order without an HT tag
    that cannot be read is skipped and counted, as in the order book (D118)."""
    tags: dict[str, tuple[str | None, str | None]] = {}
    for order in rows_of(orders):
        if isinstance(order, Mapping) and order.get("order_id") is not None:
            tags[str(order["order_id"])] = _row_tags(order.get("tag"))
    trades = TradeBook()
    for row in rows_of(rows):
        order_id = reply_id(row["order_id"], "order_id")
        tag, gtt_tag = tags[order_id]  # KeyError: the trade's order is not in the book
        try:
            trades.append(_trade_row(row, order_id, tag, gtt_tag, to_db))
        except UNREADABLE:
            if is_hat_tag(tag) or gtt_tag is not None:
                raise
            trades.skipped += 1
    return trades


def _trade_row(
    row: Mapping[str, Any],
    order_id: str,
    tag: str | None,
    gtt_tag: str | None,
    to_db: Callable[[str], str],
) -> BrokerTrade:
    return BrokerTrade(
        trade_id=reply_id(row["trade_id"], "trade_id"),
        order_id=order_id,
        tag=tag,
        symbol=_symbol(row, to_db),
        side=_side(row["transaction_type"]),
        qty=reply_count(row["quantity"], "quantity", minimum=1),
        price=require_exact(row["average_price"], "average_price", floats=True, positive=True),
        traded_at=None,
        gtt_tag=gtt_tag,
    )


def parse_positions(reply: Mapping[str, Any], to_db: Callable[[str], str]) -> list[BrokerPosition]:
    """The open positions of a `positions` reply's `net` list (a quantity of 0 is none): the
    signed quantity, Kite's average price, the product and the last price (None when missing or
    unreadable: only equity needs it). A missing or unreadable quantity, average or product
    raises."""
    rows = reply["net"]
    if not isinstance(rows, list):
        raise TypeError("net: not a list")
    positions = []
    for row in rows:
        qty = reply_count(row["quantity"], "quantity", minimum=None)
        if qty == 0:
            continue
        product = row["product"]
        if not isinstance(product, str) or not product:
            raise ValueError("product: missing")
        positions.append(
            BrokerPosition(
                symbol=_symbol(row, to_db),
                qty=qty,
                avg_price=require_exact(
                    row["average_price"], "average_price", floats=True, positive=True
                ),
                product=product,
                ltp=exact(row.get("last_price"), floats=True, positive=True),
            )
        )
    return positions


def parse_holdings(
    rows: Iterable[Mapping[str, Any]], to_db: Callable[[str], str]
) -> list[BrokerHolding]:
    """The demat holdings of a `holdings` list: the settled `quantity` plus the T1 shares not yet
    delivered (`t1_quantity`, bought the session before), as Fyers counts them [verify on a live
    account], with the last price (None when missing or unreadable: only equity needs it). A
    missing or unreadable quantity or `average_price` raises (a bonus share may cost 0; a
    negative cost is not a cost)."""
    holdings = []
    for row in rows_of(rows):
        cost = require_exact(row["average_price"], "average_price", floats=True)
        if cost < 0:
            raise ValueError("average_price: negative")
        held = reply_count(row["quantity"], "quantity") + reply_count(
            row.get("t1_quantity") or 0, "t1_quantity"
        )
        holdings.append(
            BrokerHolding(
                symbol=_symbol(row, to_db),
                qty=held,
                avg_price=cost,
                ltp=exact(row.get("last_price"), floats=True, positive=True),
            )
        )
    return holdings


def parse_funds(reply: Mapping[str, Any]) -> BrokerFunds:
    """The funds of a `margins(segment="equity")` reply: `net` is what is available to trade
    (a CNC buy's cost already out of it, as Fyers' "Available Balance"), `utilised.debits` what
    is used. A reply without them raises: it is not a reply of 0 cash."""
    available = require_exact(reply["net"], "net", floats=True)
    used = require_exact(reply["utilised"]["debits"], "debits", floats=True)
    return BrokerFunds(available=available, used=used)


def parse_gtt_row(row: Mapping[str, Any], to_db: Callable[[str], str]) -> BrokerGtt | None:
    """One GTT of `get_gtts`, or None for one that cannot be one of HAT's stops: a two-leg (OCO)
    GTT, one with other than one order, a product other than CNC, or a GTT that ended without
    selling (`cancelled`, `rejected`, `deleted`, `expired`, `disabled`). A GTT resting (`active`)
    or triggered is kept: a lost answer whose GTT has since triggered is adopted, so the fill sync
    attributes its sale. Kite tags no GTT, so `tag` is None and none is `foreign`. A single-leg CNC
    GTT whose status is unknown, or whose side, trigger or quantity cannot be read, raises, so the
    read fails instead of missing a GTT."""
    orders = row.get("orders")
    if row.get("type") != _GTT_SINGLE or not isinstance(orders, list) or len(orders) != 1:
        return None
    order = orders[0]
    if order.get("product") != "CNC":
        return None
    status = str(row.get("status") or "")
    if status in _GTT_ENDED:
        return None
    if status not in _GTT_KNOWN:
        raise ValueError("gtt status")
    condition = row["condition"]
    triggers = condition["trigger_values"]
    if not isinstance(triggers, list) or len(triggers) != 1:
        raise ValueError("gtt trigger_values")
    return BrokerGtt(
        gtt_id=reply_id(row["id"], "id"),
        symbol=_symbol(condition, to_db),
        side=_side(order["transaction_type"]),
        qty=reply_count(order["quantity"], "quantity", minimum=1),
        trigger_price=require_exact(triggers[0], "trigger", floats=True, positive=True),
    )


def _code(reply: KiteReply) -> str:
    """The fixed code of a reply that is not `ok`."""
    if reply.outcome == RATE_LIMITED:
        return CODE_RATE_LIMITED
    if reply.outcome == EXPIRED:
        return CODE_SESSION_EXPIRED
    if reply.outcome == NOT_SENT:
        return CODE_UNAVAILABLE
    return CODE_REJECTED


def ack_result(reply: KiteReply) -> AckResult:
    """The `AckResult` of a modify or cancel: ok only for Kite's `ok`; a lost answer is
    unavailable (the caller looks again), a refusal rejected."""
    if reply.outcome == OK:
        return AckResult(ok=True, code=CODE_OK)
    if reply.outcome == REFUSED:
        return AckResult(ok=False, code=CODE_REJECTED)
    if reply.outcome in (RATE_LIMITED, EXPIRED):
        return AckResult(ok=False, code=_code(reply))
    return AckResult(ok=False, code=CODE_UNAVAILABLE)


def place_result(reply: KiteReply, id_key: str | None = None) -> tuple[str, str | None, str]:
    """(status, broker id, code) of a place: `placed` with the id Kite returned (the order id
    itself, or `data[id_key]` for a GTT), `unconfirmed` for a lost or unclear answer or an `ok`
    without an id (it may have landed), and `rejected` only when nothing was placed (a refusal,
    the session's end, a rate limit, no client or an open circuit)."""
    if reply.outcome == OK:
        data = reply.data
        if id_key is not None:
            data = data.get(id_key) if isinstance(data, Mapping) else None
        if isinstance(data, bool) or not isinstance(data, str | int) or str(data).strip() == "":
            return "unconfirmed", None, CODE_UNCONFIRMED
        return "placed", str(data), CODE_OK
    if reply.outcome in (REFUSED, EXPIRED, RATE_LIMITED, NOT_SENT):
        return "rejected", None, _code(reply)
    return "unconfirmed", None, CODE_UNCONFIRMED
