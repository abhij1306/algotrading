"""Recording stand-ins for the Fyers SDK: the model (`FyersModel`) that takes orders and answers
reads, the session (`SessionModel`) that trades an auth code for a token, and the real HAT
`FyersBroker` over the model.

HAT's own copy of the fakes it needs: nothing here is imported from the legacy
test suite, and nothing reaches the real SDK or the network.
"""

from __future__ import annotations

from typing import Any

from app.hat.execution.fyers_broker import CircuitBreaker, FyersBroker

# The list each read's ok reply carries, as Fyers documents it: a default read reply has it, empty.
READ_LISTS = {
    "orderbook": "orderBook",
    "tradebook": "tradeBook",
    "holdings": "holdings",
    "positions": "netPositions",
    "gtt_orderbook": "orderBook",
    "funds": "fund_limit",
}


class RecordingSdk:
    """A fake fyers_apiv3 FyersModel: records every payload it is sent, answers from `reply`.
    With no `reply`, a write answers ok with an id and a read answers ok with its list, empty."""

    def __init__(self, reply: Any = None) -> None:  # noqa: ANN401 - raw SDK JSON
        self.calls: list[tuple[str, Any]] = []
        self.reply = reply

    def _answer(self, name: str, data: Any) -> Any:  # noqa: ANN401
        self.calls.append((name, data))
        if isinstance(self.reply, BaseException):
            raise self.reply
        if self.reply is not None:
            return self.reply
        if name in READ_LISTS:
            return {"s": "ok", "code": 200, "message": "", READ_LISTS[name]: []}
        return {"s": "ok", "code": 200, "id": f"SDK-{len(self.calls)}", "message": "ok"}

    def place_order(self, data: dict[str, Any]) -> Any:  # noqa: ANN401
        return self._answer("place_order", data)

    def modify_order(self, data: dict[str, Any]) -> Any:  # noqa: ANN401
        return self._answer("modify_order", data)

    def cancel_order(self, data: dict[str, Any]) -> Any:  # noqa: ANN401
        return self._answer("cancel_order", data)

    def place_gtt_order(self, data: dict[str, Any]) -> Any:  # noqa: ANN401
        return self._answer("place_gtt_order", data)

    def modify_gtt_order(self, data: dict[str, Any]) -> Any:  # noqa: ANN401
        return self._answer("modify_gtt_order", data)

    def cancel_gtt_order(self, data: dict[str, Any]) -> Any:  # noqa: ANN401
        return self._answer("cancel_gtt_order", data)

    def orderbook(self) -> Any:  # noqa: ANN401
        return self._answer("orderbook", None)

    def tradebook(self) -> Any:  # noqa: ANN401
        return self._answer("tradebook", None)

    def gtt_orderbook(self) -> Any:  # noqa: ANN401
        return self._answer("gtt_orderbook", None)

    def holdings(self) -> Any:  # noqa: ANN401
        return self._answer("holdings", None)

    def funds(self) -> Any:  # noqa: ANN401
        return self._answer("funds", None)

    def positions(self) -> Any:  # noqa: ANN401
        return self._answer("positions", None)

    def get_profile(self) -> Any:  # noqa: ANN401
        return self._answer("get_profile", None)


def broker_over(sdk: RecordingSdk) -> FyersBroker:
    """The real `FyersBroker` over `sdk`, with a breaker of its own (so no state leaks between
    examples)."""
    return FyersBroker(lambda: sdk, breaker=CircuitBreaker())
