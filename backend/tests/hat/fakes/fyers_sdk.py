"""A recording stand-in for the Fyers SDK model, and the real `FyersBroker` over it.

HAT's own copy of the fakes it needs (K1): nothing here is imported from the legacy test suite.
"""

from __future__ import annotations

from typing import Any

from app.brokers.plugins.fyers import FyersBroker


class RecordingSdk:
    """A fake fyers_apiv3 FyersModel: records every payload it is sent, answers from `reply`."""

    def __init__(self, reply: Any = None) -> None:  # noqa: ANN401 - raw SDK JSON
        self.calls: list[tuple[str, Any]] = []
        self.reply = reply

    def _answer(self, name: str, data: Any) -> Any:  # noqa: ANN401
        self.calls.append((name, data))
        if isinstance(self.reply, BaseException):
            raise self.reply
        if self.reply is not None:
            return self.reply
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

    def holdings(self) -> Any:  # noqa: ANN401
        return self._answer("holdings", None)

    def funds(self) -> Any:  # noqa: ANN401
        return self._answer("funds", None)

    def positions(self) -> Any:  # noqa: ANN401
        return self._answer("positions", None)

    def gtt_orderbook(self) -> Any:  # noqa: ANN401
        return self._answer("gtt_orderbook", None)


class SdkBackedBroker(FyersBroker):
    """The real `FyersBroker` over a `RecordingSdk` instead of the singleton client."""

    def __init__(self, sdk: RecordingSdk) -> None:
        self._sdk = sdk

    @property
    def client(self) -> Any:  # noqa: ANN401
        return self._sdk


def reset_circuit() -> None:
    """The breaker is class state, and a failure recorded through the subclass shadows the
    base class's value: put both back to closed between examples."""
    FyersBroker._record_success()
    SdkBackedBroker._record_success()
