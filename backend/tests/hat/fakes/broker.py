"""The one broker stand-in for the trading tests: the transport `FyersHatBroker` calls.

`FakeBroker` has the methods HAT's adapter calls on `FyersBroker` (the writes, the GTT calls and
the fail-closed `fetch_*` reads) and records each call as `(method, args)` in `calls`. It takes
the SDK payloads the adapter builds and its reads answer raw Fyers rows. Its place replies are
built by the real `classify_place_reply`, so they carry the raw reply in `details` as
`FyersBroker`'s do.

Configure it by attribute (or constructor keyword):

- `available` (the funds reply's available balance), `positions`, `tradebook`, `orders`,
  `holdings`, `gtts`, `profile` (Fyers rows) drive the reads; set one to an exception instance
  and that read raises it.
- `place_reply` is the reply of `place_order`: None (the default) accepts every order as
  `B-<n>`; an exception instance is raised. `modify_reply` and `cancel_reply` answer the order
  and GTT modifies and cancels the same way.
- `circuit_open = True` answers as `FyersBroker` does while its circuit breaker is open: places
  are rejected without being sent, modifies and cancels return the timeout fallback, and the
  reads raise `BrokerUnavailableError`.
"""

from __future__ import annotations

from typing import Any

from app.hat.execution.broker import write_refusal
from app.hat.execution.fyers_broker import (
    BROKER_CIRCUIT_OPEN,
    OrderReply,
    classify_place_reply,
)
from app.hat.execution.fyers_broker import (
    FyersUnavailableError as BrokerUnavailableError,
)

DEFAULT_FUNDS = 1_000_000.0
MODIFY_TIMEOUT = {"s": "error", "message": "Fyers modify timeout/unavailable"}
CANCEL_TIMEOUT = {"s": "error", "message": "Fyers cancel timeout/unavailable"}


def unconfirmed() -> OrderReply:
    """A place reply when the answer was lost (timeout, error mid-request)."""
    return classify_place_reply(None)


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


def _settle(outcome: Any) -> Any:  # noqa: ANN401 - replies are whatever the test set
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


class FakeBroker:
    """Broker transport stand-in that records every call. Defaults: funds 1,000,000, empty books,
    every write accepted."""

    def __init__(self, **config: object) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.available: float | BaseException = DEFAULT_FUNDS
        self.positions: list[dict[str, Any]] | BaseException = []
        self.tradebook: list[dict[str, Any]] | BaseException = []
        self.orders: list[dict[str, Any]] | BaseException = []
        self.holdings: list[dict[str, Any]] | BaseException = []
        self.gtts: list[dict[str, Any]] | BaseException = []
        self.profile: dict[str, Any] | BaseException = {"fy_id": "XA00000"}
        self.place_reply: object = None
        self.modify_reply: object = {"s": "ok", "message": "Modified"}
        self.cancel_reply: object = {"s": "ok", "message": "Cancelled"}
        self.circuit_open = False
        self.budget: Any = None  # a `CallBudget` the adapter reads `day_spent` from, or none
        for name, value in config.items():
            if name not in vars(self) or name == "calls":
                raise TypeError(f"FakeBroker has no setting {name!r}")
            setattr(self, name, value)

    def _place(self, method: str, payload: dict[str, Any], prefix: str, reply: object) -> Any:  # noqa: ANN401
        self.calls.append((method, payload))
        if self.circuit_open:
            return OrderReply(
                order_id="", status="REJECTED", message=BROKER_CIRCUIT_OPEN, details=None
            )
        if reply is not None:
            return _settle(reply)
        placed = sum(1 for name, _ in self.calls if name == method)
        return classify_place_reply({"s": "ok", "id": f"{prefix}-{placed}", "message": "Placed"})

    def write_refusal(self) -> str | None:
        """As `FyersBroker.write_refusal`: refused unsent while the circuit is open."""
        return write_refusal(self.circuit_open, writes_spent=False)

    def _write(self, method: str, args: object, reply: object, timeout: dict[str, Any]) -> Any:  # noqa: ANN401
        self.calls.append((method, args))
        return dict(timeout) if self.circuit_open else _settle(reply)

    def _read(self, method: str, state: object) -> Any:  # noqa: ANN401
        self.calls.append((method, None))
        if self.circuit_open:
            raise BrokerUnavailableError(f"Fyers {method} request failed")
        return _settle(state)

    # --- writes ---------------------------------------------------------------------------

    def place_order(self, order: dict[str, Any]) -> Any:  # noqa: ANN401 - see place_reply
        return self._place("place_order", order, "B", self.place_reply)

    def modify_order(self, order_id: str, fields: dict[str, Any]) -> Any:  # noqa: ANN401
        return self._write("modify_order", (order_id, fields), self.modify_reply, MODIFY_TIMEOUT)

    def cancel_order(self, order_id: str) -> Any:  # noqa: ANN401
        return self._write("cancel_order", order_id, self.cancel_reply, CANCEL_TIMEOUT)

    def place_gtt(self, gtt: dict[str, Any]) -> Any:  # noqa: ANN401
        return self._place("place_gtt", gtt, "G", None)

    def modify_gtt(self, gtt_id: str, fields: dict[str, Any]) -> Any:  # noqa: ANN401
        return self._write("modify_gtt", (gtt_id, fields), self.modify_reply, MODIFY_TIMEOUT)

    def cancel_gtt(self, gtt_id: str) -> Any:  # noqa: ANN401
        return self._write("cancel_gtt", gtt_id, self.cancel_reply, CANCEL_TIMEOUT)

    # --- fail-closed reads (FyersBroker.fetch_*) --------------------------------------------

    def fetch_orderbook(self) -> list[dict[str, Any]]:
        return self._read("fetch_orderbook", self.orders)

    def fetch_tradebook(self) -> list[dict[str, Any]]:
        return self._read("fetch_tradebook", self.tradebook)

    def fetch_holdings(self) -> list[dict[str, Any]]:
        return self._read("fetch_holdings", self.holdings)

    def fetch_positions(self) -> list[dict[str, Any]]:
        return self._read("fetch_positions", self.positions)

    def fetch_gtt_orders(self) -> list[dict[str, Any]]:
        return self._read("fetch_gtt_orders", self.gtts)

    def fetch_funds(self) -> dict[str, Any]:
        available = self._read("fetch_funds", self.available)
        return available if isinstance(available, dict) else funds_reply(available)

    def get_profile(self) -> dict[str, Any]:
        return self._read("get_profile", self.profile)
