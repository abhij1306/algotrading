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
  `rejected` and `unconfirmed` (the reply of a timeout or a lost answer).

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


def unconfirmed() -> dict[str, Any]:
    """A place_order reply when the answer was lost (timeout, error mid-request)."""
    return {
        "order_id": "",
        "status": SUBMIT_UNCONFIRMED,
        "message": BROKER_ORDER_UNCONFIRMED,
        "details": None,
    }


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
        return self._read("find_order_by_tag", order_tag, self.tagged_order, fail_closed=True)

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
