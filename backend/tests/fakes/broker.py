"""One configurable broker stand-in for every trading test.

`FakeBroker` has the methods the app calls on `FyersBroker` and records each call as
`(method, args)` in `calls`: the single argument itself, a tuple when there are several,
`None` when there are none. Use the `fake_broker` fixture (tests/conftest.py) to put it
behind every broker handle the app holds.

Configure it in three ways (attributes can also be passed to the constructor):

- State attributes drive the default replies: `ltp`, `available`, `positions`, `working`,
  `tradebook`, `orders`, `tagged_order`. Set any of them to an exception instance to make
  that read raise it on every call.
- `modify_reply` (default `{"s": "ok", ...}`), `cancel_reply` (default `{"s": "ok"}`) and
  `place_reply` are the replies of the writes; an exception instance is raised instead.
  `place_reply = None` (the default) accepts every order as `B-<n>` (with `place_message`
  when set). Any accepted place (SUBMITTED with an id) is added to `working`.
- `script(method, *outcomes)` queues one outcome per call, ahead of everything above:
  each is returned, or raised when it is an exception. Build them with `accepted`,
  `rejected` and `unconfirmed` (the reply of a timeout or a lost answer). `unconfirmed(landed=True)`
  is a lost answer for an order that did reach the broker.

Tags and GTT (the HAT adapter's needs): every accepted or landed place that carries an
`order_tag` is kept in `book` as a Fyers orderbook row, and `find_order_by_tag` and
`fetch_orderbook` read it (`tagged_order`, when set, still wins). `fetch_*` are the fail-closed
reads (they raise `BrokerUnavailableError` on `circuit_open` or when their state is an exception,
as `FyersBroker.fetch_*` do). `place_gtt`, `modify_gtt` and `cancel_gtt` record into `gtts`;
`place_gtt_reply` scripts the GTT place reply like `place_reply` does for orders.

`circuit_open = True` answers as `FyersBroker` does while its circuit breaker is open:
places are rejected without being sent, modify and cancel return the timeout fallback,
and the reads that fail closed raise `BrokerUnavailableError`.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from app.brokers.base import BrokerUnavailableError
from app.brokers.plugins.fyers import (
    BROKER_CIRCUIT_OPEN,
    BROKER_ORDER_UNCONFIRMED,
    FYERS_ORDER_STATUSES,
    SUBMIT_UNCONFIRMED,
)

DEFAULT_LTP = 800.0
DEFAULT_FUNDS = 1_000_000.0


def accepted(order_id: str, message: str | None = None) -> dict[str, Any]:
    """A place_order reply for an order the broker took."""
    reply: dict[str, Any] = {"status": "SUBMITTED", "order_id": order_id}
    if message is not None:
        reply["message"] = message
    return reply


def rejected(message: str) -> dict[str, Any]:
    """A place_order reply for a definite rejection (nothing was placed)."""
    return {"order_id": "", "status": "REJECTED", "message": message, "details": None}


class _Landed(dict[str, Any]):
    """A lost-answer reply for an order that did reach the broker."""


def unconfirmed(landed: bool = False) -> dict[str, Any]:
    """A place_order reply when the answer was lost (timeout, error mid-request). With
    `landed=True` the order did reach the broker: it shows up in `book` and by tag."""
    reply = {
        "order_id": "",
        "status": SUBMIT_UNCONFIRMED,
        "message": BROKER_ORDER_UNCONFIRMED,
        "details": None,
    }
    return _Landed(reply) if landed else reply


def broker_position(symbol: str, net_qty: int, product: str = "INTRADAY") -> dict[str, Any]:
    """A position as FyersBroker.get_positions() returns it (entry 790, LTP 800)."""
    return {
        "symbol": symbol,
        "side": "LONG" if net_qty > 0 else "SHORT",
        "quantity": abs(net_qty),
        "entry_price": 790.0,
        "current_price": 800.0,
        "pnl": 100.0,
        "product_type": product,
    }


def _settle(outcome: Any) -> Any:  # noqa: ANN401 - replies are whatever the test scripted
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


class FakeBroker:
    """Broker stand-in that records every call. Defaults: LTP 800, funds 1,000,000, no
    positions, no working orders, every write accepted."""

    def __init__(self, **config: object) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.ltp: float | BaseException | None = DEFAULT_LTP
        self.available: float | BaseException = DEFAULT_FUNDS
        self.positions: list[dict[str, Any]] | BaseException = []
        self.working: list[dict[str, Any]] | BaseException = []
        self.tradebook: list[dict[str, Any]] | BaseException = []
        self.orders: list[dict[str, Any]] | BaseException = []
        self.tagged_order: dict[str, Any] | BaseException | None = None
        self.holdings: list[dict[str, Any]] | BaseException = []
        self.book: list[dict[str, Any]] = []
        self.gtts: list[dict[str, Any]] = []
        self.place_gtt_reply: object = None
        self.place_reply: object = None
        self.place_message: str | None = None
        self.modify_reply: object = {"s": "ok", "message": "Modified"}
        self.cancel_reply: object = {"s": "ok"}
        self.circuit_open = False
        self._scripts: dict[str, deque[object]] = {}
        for name, value in config.items():
            if name not in vars(self) or name.startswith("_") or name == "calls":
                raise TypeError(f"FakeBroker has no setting {name!r}")
            setattr(self, name, value)

    # --- configuration and inspection -------------------------------------------------

    def script(self, method: str, *outcomes: object) -> None:
        """Queue one outcome per call of `method`, used before any other configuration."""
        self._scripts.setdefault(method, deque()).extend(outcomes)

    def count(self, name: str) -> int:
        return len(self.calls_named(name))

    def calls_named(self, name: str) -> list[Any]:
        return [args for call, args in self.calls if call == name]

    def _call(self, method: str, args: object) -> deque[object] | None:
        """Record the call; return its script queue when an outcome is waiting."""
        self.calls.append((method, args))
        queue = self._scripts.get(method)
        return queue if queue else None

    # --- writes -----------------------------------------------------------------------

    def place_order(self, order: dict[str, Any]) -> Any:  # noqa: ANN401 - see place_reply
        queue = self._call("place_order", order)
        if queue:
            reply = _settle(queue.popleft())
        elif self.circuit_open:
            reply = rejected(BROKER_CIRCUIT_OPEN)
        elif self.place_reply is not None:
            reply = _settle(self.place_reply)
        else:
            reply = accepted(f"B-{self.count('place_order')}", self.place_message)
        landed = isinstance(reply, _Landed) or (
            isinstance(reply, dict) and reply.get("status") == "SUBMITTED" and reply.get("order_id")
        )
        if landed and order.get("order_tag"):
            self.book.append(self._book_row(order, reply))
        if (
            isinstance(reply, dict)
            and reply.get("status") == "SUBMITTED"
            and reply.get("order_id")
            and isinstance(self.working, list)
        ):
            # An accepted order keeps working at the broker until it fills.
            self.working.append(
                {
                    "symbol": order["symbol"],
                    "side": order["side"],
                    "quantity": order["quantity"],
                    "product_type": order["product"],
                }
            )
        return reply

    def _book_row(self, order: dict[str, Any], reply: dict[str, Any]) -> dict[str, Any]:
        """The Fyers orderbook row of a placed order (pending, nothing filled)."""
        return {
            "id": reply.get("order_id") or f"L-{len(self.book) + 1}",
            "orderTag": order["order_tag"],
            "symbol": order["symbol"],
            "side": 1 if order["side"] == "BUY" else -1,
            "qty": order["quantity"],
            "filledQty": 0,
            "status": 6,
            "productType": order["product"],
            "type": order["type"],
        }

    def modify_order(self, order_id: str, params: dict[str, Any]) -> Any:  # noqa: ANN401
        queue = self._call("modify_order", (order_id, params))
        if queue:
            return _settle(queue.popleft())
        if self.circuit_open:
            return {"s": "error", "message": "Fyers modify timeout/unavailable"}
        return _settle(self.modify_reply)

    def cancel_order(self, order_id: str) -> Any:  # noqa: ANN401
        queue = self._call("cancel_order", order_id)
        if queue:
            return _settle(queue.popleft())
        if self.circuit_open:
            return {"s": "error", "message": "Fyers cancel timeout/unavailable"}
        return _settle(self.cancel_reply)

    # --- reads ------------------------------------------------------------------------

    def _read(self, method: str, args: object, state: object, *, fail_closed: bool) -> Any:  # noqa: ANN401
        queue = self._call(method, args)
        if queue:
            return _settle(queue.popleft())
        if self.circuit_open and fail_closed:
            raise BrokerUnavailableError(f"Fyers {method} request failed")
        return _settle(state)

    def find_order_by_tag(self, order_tag: str) -> dict[str, Any] | None:
        found = self.tagged_order
        if found is None:
            found = next(
                (
                    {"order_id": row["id"], "status": FYERS_ORDER_STATUSES[row["status"]]}
                    for row in self.book
                    if row["orderTag"] == order_tag
                ),
                None,
            )
        return self._read("find_order_by_tag", order_tag, found, fail_closed=True)

    def get_holdings(self) -> list[dict[str, Any]]:
        return self._read(
            "get_holdings", None, [] if self.circuit_open else self.holdings, fail_closed=False
        )

    # --- fail-closed reads (FyersBroker.fetch_*) ----------------------------------------

    def fetch_orderbook(self) -> list[dict[str, Any]]:
        rows = self._read("fetch_orderbook", None, self.orders, fail_closed=True)
        return [*rows, *self.book]

    def fetch_tradebook(self) -> list[dict[str, Any]]:
        return self._read("fetch_tradebook", None, self.tradebook, fail_closed=True)

    def fetch_holdings(self) -> list[dict[str, Any]]:
        return self._read("fetch_holdings", None, self.holdings, fail_closed=True)

    def fetch_funds(self) -> dict[str, float]:
        available = self._read("fetch_funds", None, self.available, fail_closed=True)
        if isinstance(available, dict):  # a scripted funds reply
            return available
        return {"available": available, "used": 0.0, "total": available}

    def fetch_gtt_orders(self) -> list[dict[str, Any]]:
        return self._read("fetch_gtt_orders", None, self.gtts, fail_closed=True)

    # --- GTT writes ---------------------------------------------------------------------

    def place_gtt(self, gtt: dict[str, Any]) -> Any:  # noqa: ANN401 - see place_gtt_reply
        queue = self._call("place_gtt", gtt)
        if queue:
            reply = _settle(queue.popleft())
        elif self.circuit_open:
            reply = rejected(BROKER_CIRCUIT_OPEN)
        elif self.place_gtt_reply is not None:
            reply = _settle(self.place_gtt_reply)
        else:
            reply = accepted(f"G-{self.count('place_gtt')}")
        if isinstance(reply, _Landed) or (
            isinstance(reply, dict) and reply.get("status") == "SUBMITTED" and reply.get("order_id")
        ):
            self.gtts.append({**gtt, "id": reply.get("order_id") or f"GL-{len(self.gtts) + 1}"})
        return reply

    def modify_gtt(self, gtt_id: str, params: dict[str, Any]) -> Any:  # noqa: ANN401
        queue = self._call("modify_gtt", (gtt_id, params))
        if queue:
            return _settle(queue.popleft())
        if self.circuit_open:
            return {"s": "error", "message": "Fyers modify timeout/unavailable"}
        return {"s": "ok", "message": "Modified"}

    def cancel_gtt(self, gtt_id: str) -> Any:  # noqa: ANN401
        queue = self._call("cancel_gtt", gtt_id)
        if queue:
            return _settle(queue.popleft())
        if self.circuit_open:
            return {"s": "error", "message": "Fyers cancel timeout/unavailable"}
        return {"s": "ok"}

    def get_positions(self) -> list[dict[str, Any]]:
        return self._read("get_positions", None, self.positions, fail_closed=True)

    def get_working_orders(self) -> list[dict[str, Any]]:
        return self._read("get_working_orders", None, self.working, fail_closed=True)

    def get_orders(self) -> list[dict[str, Any]]:
        return self._read(
            "get_orders", None, [] if self.circuit_open else self.orders, fail_closed=False
        )

    def get_tradebook(self) -> list[dict[str, Any]]:
        return self._read(
            "get_tradebook", None, [] if self.circuit_open else self.tradebook, fail_closed=False
        )

    def get_funds(self) -> dict[str, float]:
        available = self._read("get_funds", None, self.available, fail_closed=False)
        if isinstance(available, dict):  # a scripted funds reply
            return available
        if self.circuit_open:
            available = 0.0
        return {"available": available, "used": 0.0, "total": available}

    def get_quote(self, symbol: str) -> dict[str, Any]:
        ltp = self._read("get_quote", symbol, self.ltp, fail_closed=False)
        if isinstance(ltp, dict):  # a scripted quote
            return ltp
        if self.circuit_open or not ltp or ltp <= 0:
            return {}
        return {"n": symbol, "v": {"lp": ltp}}
