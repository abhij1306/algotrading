"""HAT's own broker contract (HAT-BRK-080, HAT-DEC-I7, HAT-BRK-081).

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
    "CODES_NOT_SENT",
    "AckResult",
    "BrokerError",
    "BrokerFunds",
    "BrokerGtt",
    "BrokerHolding",
    "BrokerOrder",
    "BrokerPosition",
    "BrokerTrade",
    "BrokerUnavailableError",
    "Capabilities",
    "GttModify",
    "GttRequest",
    "GttResult",
    "HatBroker",
    "InvalidIntentError",
    "OrderBook",
    "OrderIntent",
    "OrderType",
    "PlaceResult",
    "SessionStatus",
    "Side",
    "TradeBook",
    "write_refusal",
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
# a write refused with one of these was not acted on by the broker (no circuit, no client, or the
# day's requests spent): not a rejection, so it burns no retry and is tried again later
CODES_NOT_SENT = frozenset({CODE_UNAVAILABLE, CODE_RATE_LIMITED})


def write_refusal(circuit_open: bool, writes_spent: bool) -> str | None:
    """The code a write is refused with before it is sent: the circuit is open, or the day's
    writes are spent (whichever told the broker so); None when one may go."""
    if circuit_open:
        return CODE_UNAVAILABLE
    return CODE_RATE_LIMITED if writes_spent else None


class BrokerError(HatError):
    """A broker failure. `str(error)` is `code`, a fixed word, never the broker's own text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BrokerUnavailableError(BrokerError):
    """The broker could not answer (timeout, circuit open, error payload, no session)."""

    def __init__(self, code: str = CODE_UNAVAILABLE) -> None:
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

    def to_json(self) -> dict[str, object]:
        return asdict(self)


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
    at `limit_price`. `tag` is the stop's HT tag, which the broker shows on the GTT."""

    symbol: str
    qty: int
    trigger_price: Decimal
    limit_price: Decimal
    product: str = "CNC"
    side: Side = Side.SELL
    tag: str | None = None


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
    # The HT tag of the GTT this order is the sale of, when one of HAT's GTTs triggered into it
    # (its `tag` is None then: it is the GTT's child, not an order HAT placed).
    gtt_tag: str | None = None


class OrderBook(list[BrokerOrder]):
    """The broker's order book as read: the readable rows, plus how many rows of orders outside
    HAT's scope (no HT tag) could not be read and were skipped (D118)."""

    skipped: int = 0


@dataclass(frozen=True)
class BrokerGtt:
    """A single-leg CNC GTT in the broker's GTT book, resting or already triggered. `tag` is the
    HT tag of one of HAT's GTTs it was placed with, else None; `foreign` marks one shown under
    some other tag (manual, or another purpose's): HAT tells its own by the tag, an untagged one
    by the symbol, side, quantity and trigger it sent, and never adopts a foreign one."""

    gtt_id: str
    symbol: str  # database symbol
    side: Side
    qty: int
    trigger_price: Decimal
    tag: str | None = None
    foreign: bool = False


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
    gtt_tag: str | None = None  # as BrokerOrder.gtt_tag


class TradeBook(list[BrokerTrade]):
    """The broker's trade book as read: the readable rows, plus how many trades outside HAT's
    scope (no HT tag, no tag of one of HAT's GTTs) could not be read and were skipped (D118)."""

    skipped: int = 0


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: int  # signed: long positive
    avg_price: Decimal
    product: str
    ltp: Decimal | None = None  # the broker's last traded price; None when it gave none


@dataclass(frozen=True)
class BrokerHolding:
    symbol: str
    qty: int
    avg_price: Decimal
    ltp: Decimal | None = None  # the broker's last traded price; None when it gave none


@dataclass(frozen=True)
class BrokerFunds:
    """The cash of the account: `available` to trade, and `used` (a CNC buy's cost sits there
    until settlement). Equity is `available` plus the shares at market (`equity.account_equity`,
    Q1), never their sum."""

    available: Decimal
    used: Decimal


@runtime_checkable
class HatBroker(Protocol):
    """The methods of exactly."""

    def capabilities(self) -> Capabilities: ...

    def day_budget_spent(self) -> bool:
        """Whether the trading day's broker calls are spent (Q11): a local value, no call."""
        ...

    def write_refusal(self) -> str | None:
        """The code a write would be refused with now, unsent (`write_refusal`): a local value,
        no call; None when one may go."""
        ...

    def claim_unreachable_alert(self, after: float) -> float | None:
        """The seconds the broker's calls have failed in a row (its circuit breaker's outage),
        once per outage when it has lasted `after`, whichever account asks; None otherwise. A
        local value, no call."""
        ...

    def release_unreachable_alert(self) -> None:
        """Give back a claim whose alert was not delivered."""
        ...

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

    def trades(self) -> TradeBook: ...

    def positions(self) -> list[BrokerPosition]: ...

    def holdings(self) -> list[BrokerHolding]: ...

    def funds(self) -> BrokerFunds: ...
