"""HAT's own broker contract (HAT-BRK-080, HAT-DEC-I7, HAT-BRK-081; 07 §8).

Engine, risk and router code depend only on this module: never on `FyersBroker`, the fyers SDK
or any OpenAlgo client. No adapter-specific type crosses it, and no result carries raw broker
text: failures are a fixed code (`CODE_*`) or a `BrokerError` whose message is that code.

Read methods raise `BrokerUnavailableError` when the broker cannot answer, never an empty list
(trading-safety rule 7). Prices and quantities are `Decimal` and `int`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ..errors import HatError

__all__ = [
    "CODE_INVALID_INTENT",
    "CODE_OK",
    "CODE_RATE_LIMITED",
    "CODE_REJECTED",
    "CODE_SESSION_EXPIRED",
    "CODE_UNAVAILABLE",
    "CODE_UNCONFIRMED",
    "AckResult",
    "BrokerError",
    "BrokerFunds",
    "BrokerGtt",
    "BrokerHolding",
    "BrokerLoginError",
    "BrokerOrder",
    "BrokerPosition",
    "BrokerTrade",
    "BrokerUnavailableError",
    "Capabilities",
    "GttModify",
    "GttRequest",
    "GttResult",
    "HatBroker",
    "Instrument",
    "InvalidIntentError",
    "OrderBook",
    "OrderIntent",
    "OrderType",
    "OrderUpdate",
    "PlaceResult",
    "SessionStatus",
    "Side",
]

CODE_OK = "ok"
CODE_REJECTED = "broker_rejected"
CODE_UNAVAILABLE = "broker_unavailable"
CODE_SESSION_EXPIRED = "broker_session_expired"  # the broker refused the token: log in again
# the broker refused the call because the plan's daily requests are spent (Fyers -360): not an
# outage, and no call succeeds until the next day
CODE_RATE_LIMITED = "broker_rate_limited"
CODE_UNCONFIRMED = "submit_unconfirmed"
CODE_INVALID_INTENT = "invalid_intent"


class BrokerError(HatError):
    """A broker failure. `str(error)` is `code`, a fixed word, never the broker's own text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BrokerUnavailableError(BrokerError):
    """The broker could not answer (timeout, circuit open, error payload, no session)."""

    def __init__(self, code: str = CODE_UNAVAILABLE) -> None:
        super().__init__(code)


class BrokerLoginError(BrokerError):
    """The login code was refused or the token could not be stored."""

    def __init__(self, code: str = "login_failed") -> None:
        super().__init__(code)


class InvalidIntentError(BrokerError):
    """An intent the adapter refuses before any SDK call."""

    def __init__(self, code: str = CODE_INVALID_INTENT) -> None:
        super().__init__(code)


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL-M"


class SessionStatus(StrEnum):
    """Whether the broker session can place orders. Only LIVE can."""

    LIVE = "live"
    EXPIRED = "expired"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Capabilities:
    gtt_delivery: bool
    tag_max_len: int
    tag_alphabet: str  # regex character class body the broker accepts in a tag
    order_types: tuple[str, ...]
    products: tuple[str, ...]
    static_ip_required: bool

    def to_json(self) -> dict[str, object]:
        data = asdict(self)
        data["order_types"] = list(self.order_types)
        data["products"] = list(self.products)
        return data


@dataclass(frozen=True)
class OrderIntent:
    """One order to place. `symbol` is the database symbol (`SBIN`); the adapter converts it."""

    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    tag: str
    product: str = "CNC"
    price: Decimal | None = None
    trigger_price: Decimal | None = None


@dataclass(frozen=True)
class PlaceResult:
    """`status`: placed (broker order id known), rejected (definitely not placed) or
    unconfirmed (it may have reached the broker: resolve by tag before any retry)."""

    status: str
    broker_order_id: str | None
    code: str


@dataclass(frozen=True)
class AckResult:
    ok: bool
    code: str


@dataclass(frozen=True)
class GttRequest:
    """A protective sell-stop for delivery: triggers at `trigger_price`, then a limit order
    at `limit_price`."""

    symbol: str
    qty: int
    trigger_price: Decimal
    limit_price: Decimal
    product: str = "CNC"
    side: Side = Side.SELL


@dataclass(frozen=True)
class GttModify:
    gtt_id: str
    qty: int
    trigger_price: Decimal
    limit_price: Decimal


@dataclass(frozen=True)
class GttResult:
    status: str  # placed | rejected | unconfirmed
    gtt_id: str | None
    code: str


@dataclass(frozen=True)
class BrokerOrder:
    order_id: str
    tag: str | None
    symbol: str  # database symbol
    side: Side
    qty: int
    filled_qty: int
    status: str  # one of hat_order.status
    product: str
    order_type: OrderType | None = None  # None when the broker's code is unknown
    price: Decimal | None = None  # the limit price; None for an order without one


class OrderBook(list[BrokerOrder]):
    """The broker's order book as read: the readable rows, plus how many rows of orders outside
    HAT's scope (no HT tag) could not be read and were skipped (D118)."""

    skipped: int = 0


@dataclass(frozen=True)
class BrokerGtt:
    """A single-leg CNC GTT in the broker's GTT book, resting or already triggered. GTTs carry
    no tag: HAT tells its own from the symbol, side, quantity and trigger it sent."""

    gtt_id: str
    symbol: str  # database symbol
    side: Side
    qty: int
    trigger_price: Decimal


@dataclass(frozen=True)
class BrokerTrade:
    trade_id: str
    order_id: str
    tag: str | None
    symbol: str
    side: Side
    qty: int
    price: Decimal
    traded_at: datetime | None


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: int  # signed: long positive
    avg_price: Decimal
    product: str


@dataclass(frozen=True)
class BrokerHolding:
    symbol: str
    qty: int
    avg_price: Decimal


@dataclass(frozen=True)
class BrokerFunds:
    available: Decimal
    used: Decimal
    total: Decimal


@dataclass(frozen=True)
class Instrument:
    symbol: str  # database symbol
    broker_symbol: str


@dataclass(frozen=True)
class OrderUpdate:
    order_id: str
    tag: str | None
    status: str
    filled_qty: int


@runtime_checkable
class HatBroker(Protocol):
    """The methods of 07 §8, exactly."""

    def capabilities(self) -> Capabilities: ...

    def login_url(self) -> str: ...

    def complete_login(self, code: str) -> SessionStatus: ...

    def session_status(self) -> SessionStatus: ...

    def place_order(self, intent: OrderIntent) -> PlaceResult: ...

    def modify_order(
        self,
        broker_order_id: str,
        *,
        order_type: OrderType,
        qty: int | None = None,
        price: Decimal | None = None,
        trigger_price: Decimal | None = None,
    ) -> AckResult: ...

    def cancel_order(self, broker_order_id: str) -> AckResult: ...

    def place_gtt_stop(self, request: GttRequest) -> GttResult: ...

    def modify_gtt(self, request: GttModify) -> AckResult: ...

    def cancel_gtt(self, gtt_id: str) -> AckResult: ...

    def gtts(self) -> list[BrokerGtt]: ...

    def orders(self) -> OrderBook: ...

    def trades(self) -> list[BrokerTrade]: ...

    def positions(self) -> list[BrokerPosition]: ...

    def holdings(self) -> list[BrokerHolding]: ...

    def funds(self) -> BrokerFunds: ...

    def instruments(self) -> list[Instrument]: ...

    def order_updates(self) -> list[OrderUpdate]: ...
