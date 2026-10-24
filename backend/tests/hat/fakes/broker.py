"""One configurable broker stand-in for every trading test.

`FakeBroker` has the methods HAT's adapter calls on `FyersBroker`, the transport (the writes, the
GTT calls and the fail-closed `fetch_*` reads), and records each call as `(method, args)` in
`calls`: the single argument itself, a tuple when there are several, `None` when there are none.
Like the transport, it takes the SDK payloads the adapter builds (`side` 1 or -1, `type` the Fyers
code, `qty`, `limitPrice`, `stopPrice`, `orderTag`; a GTT's leg in `orderInfo.leg1`; a modify's
fields besides the id) and its reads answer raw Fyers rows (PR 75d). K4 removed the lenient reads
and the tag lookup that only the manual-trading code called.

Configure it in three ways (attributes can also be passed to the constructor):

- State attributes drive the default replies: `available` (the funds reply's available and total
  balance), `positions` (`netPositions` rows), `tradebook`, `orders`, `holdings` (Fyers rows).
  Set any of them to an exception instance to make that read raise it on every call.
- `modify_reply` (default `{"s": "ok", ...}`), `cancel_reply` (default `{"s": "ok"}`) and
  `place_reply` are the replies of the writes; an exception instance is raised instead.
  `place_reply = None` (the default) accepts every order as `B-<n>` (with `place_message`
  when set).
- `script(method, *outcomes)` queues one outcome per call, ahead of everything above:
  each is returned, or raised when it is an exception. Build them with `accepted`,
  `rejected` and `unconfirmed` (the reply of a timeout or a lost answer). `unconfirmed(landed=True)`
  is a lost answer for an order that did reach the broker.

Tags and GTT (the HAT adapter's needs): every accepted or landed place that carries an
`order_tag` is kept in `book` as a Fyers orderbook row, and `fetch_orderbook` reads it. `fetch_*`
are the fail-closed reads (they raise `BrokerUnavailableError` on `circuit_open` or when their
state is an exception, as `FyersBroker.fetch_*` do). `place_gtt`, `modify_gtt` and `cancel_gtt`
keep `gtts`, the Fyers GTT book rows `fetch_gtt_orders` reads (a cancelled GTT stays in the book
with `ord_status` 1; `resting_gtts()` lists the ones still active); `place_gtt_reply` scripts
the GTT place reply like `place_reply` does for orders.

`circuit_open = True` answers as `FyersBroker` does while its circuit breaker is open:
places are rejected without being sent, modify and cancel return the timeout fallback,
and the reads that fail closed raise `BrokerUnavailableError`.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from app.hat.execution.fyers_broker import (
    BROKER_CIRCUIT_OPEN,
    BROKER_ORDER_UNCONFIRMED,
    SUBMIT_UNCONFIRMED,
)
from app.hat.execution.fyers_broker import (
    FyersUnavailableError as BrokerUnavailableError,
)

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


def funds_reply(available: object, used: object = 0.0) -> dict[str, Any]:
    """An ok Fyers funds reply whose "Available Balance" is `available` and "Utilized Amount"
    `used` ("Total Balance" their sum, when both are numbers)."""
    rows = [
        {"title": "Available Balance", "equityAmount": available},
        {"title": "Utilized Amount", "equityAmount": used},
    ]
    if isinstance(available, int | float) and isinstance(used, int | float):
        rows.append({"title": "Total Balance", "equityAmount": available + used})
    return {"s": "ok", "code": 200, "message": "", "fund_limit": rows}


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


def _settle(outcome: Any) -> Any:  # noqa: ANN401 - replies are whatever the test scripted
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


class FakeBroker:
    """Broker stand-in that records every call. Defaults: LTP 800, funds 1,000,000, no
    positions, no working orders, every write accepted."""

    def __init__(self, **config: object) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.available: float | BaseException = DEFAULT_FUNDS
        self.positions: list[dict[str, Any]] | BaseException = []
        self.tradebook: list[dict[str, Any]] | BaseException = []
        self.orders: list[dict[str, Any]] | BaseException = []
        self.holdings: list[dict[str, Any]] | BaseException = []
        self.profile: dict[str, Any] | BaseException = {"fy_id": "XA00000"}
        self.book: list[dict[str, Any]] = []
        # How many of the newest `book` rows the order book does not show yet (Fyers' book can
        # lag a placed order: ).
        self.lagging = 0
        self.gtts: list[dict[str, Any]] = []
        self.place_gtt_reply: object = None
        self.place_reply: object = None
        self.place_message: str | None = None
        self.modify_reply: object = {"s": "ok", "message": "Modified"}
        self.cancel_reply: object = {"s": "ok"}
        self.circuit_open = False
        self.budget: Any = None  # a `CallBudget` the adapter reads `day_spent` from, or none
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
        if landed and order.get("orderTag"):
            self.book.append(self._book_row(order, reply))
        return reply

    def _book_row(self, order: dict[str, Any], reply: dict[str, Any]) -> dict[str, Any]:
        """The Fyers orderbook row of a placed order (pending, nothing filled)."""
        return {
            "id": reply.get("order_id") or f"L-{len(self.book) + 1}",
            "orderTag": order["orderTag"],
            "symbol": order["symbol"],
            "side": order["side"],
            "qty": order["qty"],
            "filledQty": 0,
            "status": 6,
            "productType": order["productType"],
            "type": order["type"],
        }

    def modify_order(self, order_id: str, fields: dict[str, Any]) -> Any:  # noqa: ANN401
        queue = self._call("modify_order", (order_id, fields))
        if queue:
            return _settle(queue.popleft())
        if self.circuit_open:
            return {"s": "error", "message": "Fyers modify timeout/unavailable"}
        reply = _settle(self.modify_reply)
        if isinstance(reply, dict) and reply.get("s") == "ok":
            # the broker shows a modified order with its new fields
            shown = {
                k: v for k, v in fields.items() if k in ("type", "qty", "limitPrice", "stopPrice")
            }
            rows = [*self.book, *(self.orders if isinstance(self.orders, list) else [])]
            for row in rows:
                if row["id"] == order_id:
                    row.update(shown)
        return reply

    def cancel_order(self, order_id: str) -> Any:  # noqa: ANN401
        queue = self._call("cancel_order", order_id)
        if queue:
            return _settle(queue.popleft())
        if self.circuit_open:
            return {"s": "error", "message": "Fyers cancel timeout/unavailable"}
        reply = _settle(self.cancel_reply)
        if isinstance(reply, dict) and reply.get("s") == "ok":
            for row in self.book:  # the broker shows a cancelled order as cancelled
                if row["id"] == order_id and row["status"] in (4, 6):
                    row["status"] = 1
        return reply

    # --- reads ------------------------------------------------------------------------

    def _read(self, method: str, args: object, state: object, *, fail_closed: bool) -> Any:  # noqa: ANN401
        queue = self._call(method, args)
        if queue:
            return _settle(queue.popleft())
        if self.circuit_open and fail_closed:
            raise BrokerUnavailableError(f"Fyers {method} request failed")
        return _settle(state)

    # --- fail-closed reads (FyersBroker.fetch_*) ----------------------------------------

    def fetch_orderbook(self) -> list[dict[str, Any]]:
        rows = self._read("fetch_orderbook", None, self.orders, fail_closed=True)
        return [*rows, *self.book[: max(0, len(self.book) - self.lagging)]]

    def fetch_tradebook(self) -> list[dict[str, Any]]:
        return self._read("fetch_tradebook", None, self.tradebook, fail_closed=True)

    def fetch_holdings(self) -> list[dict[str, Any]]:
        return self._read("fetch_holdings", None, self.holdings, fail_closed=True)

    def fetch_funds(self) -> dict[str, Any]:
        available = self._read("fetch_funds", None, self.available, fail_closed=True)
        if isinstance(available, dict):  # a scripted funds reply
            return available
        return funds_reply(available)

    def fetch_positions(self) -> list[dict[str, Any]]:
        return self._read("fetch_positions", None, self.positions, fail_closed=True)

    def fetch_gtt_orders(self) -> list[dict[str, Any]]:
        return self._read("fetch_gtt_orders", None, self.gtts, fail_closed=True)

    def get_profile(self) -> dict[str, Any]:
        return self._read("get_profile", None, self.profile, fail_closed=True)

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
            self.gtts.append(
                self._gtt_row(gtt, reply.get("order_id") or f"GL-{len(self.gtts) + 1}")
            )
        return reply

    @staticmethod
    def _gtt_row(gtt: dict[str, Any], gtt_id: str) -> dict[str, Any]:
        """The Fyers GTT book row of a placed GTT, in the shape recorded live on 2026-10-22: one
        row per GTT, its leg flat on the row, `tran_side`, `product_type`, `ordertag`
        ("1:GTT<tag>", or "2:GTTUntagged" for a GTT placed without one), `ord_status` 6
        (pending) and `gtt_oco_ind` 1 (a single GTT)."""
        tag = gtt.get("orderTag")
        leg = gtt["orderInfo"]["leg1"]
        return {
            "id": gtt_id,
            "symbol": gtt["symbol"],
            "tran_side": gtt["side"],
            "product_type": gtt["productType"],
            "ordertag": f"1:GTT{tag}" if tag else "2:GTTUntagged",
            "gtt_oco_ind": 1,
            "ord_status": 6,
            "price_limit": leg["price"],
            "price_trigger": leg["triggerPrice"],
            "qty": leg["qty"],
        }

    def resting_gtts(self) -> list[dict[str, Any]]:
        """The GTT book rows that can still trigger (no status, 4 transit or 6 pending, as the
        adapter reads them)."""
        return [row for row in self.gtts if row.get("ord_status") in (None, 4, 6)]

    def modify_gtt(self, gtt_id: str, fields: dict[str, Any]) -> Any:  # noqa: ANN401
        queue = self._call("modify_gtt", (gtt_id, fields))
        if queue:
            return _settle(queue.popleft())
        if self.circuit_open:
            return {"s": "error", "message": "Fyers modify timeout/unavailable"}
        reply = _settle(self.modify_reply)
        if not (isinstance(reply, dict) and reply.get("s") == "ok"):
            return reply
        leg = fields["orderInfo"]["leg1"]
        for row in self.resting_gtts():
            if row["id"] == gtt_id:
                row.update(
                    price_limit=leg["price"], price_trigger=leg["triggerPrice"], qty=leg["qty"]
                )
        return reply

    def cancel_gtt(self, gtt_id: str) -> Any:  # noqa: ANN401
        queue = self._call("cancel_gtt", gtt_id)
        if queue:
            return _settle(queue.popleft())
        if self.circuit_open:
            return {"s": "error", "message": "Fyers cancel timeout/unavailable"}
        for row in self.gtts:
            if row["id"] == gtt_id:
                row["ord_status"] = 1
        return {"s": "ok"}
