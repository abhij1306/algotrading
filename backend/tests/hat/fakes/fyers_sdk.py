"""Recording stand-ins for the Fyers SDK: the model (`FyersModel`) that takes orders and answers
reads, the session (`SessionModel`) that trades an auth code for a token, and the real HAT
`FyersBroker` over the model.

HAT's own copy of the fakes it needs (K1, repointed in K2): nothing here is imported from the legacy
test suite, and nothing reaches the real SDK or the network.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.hat.execution.fyers_broker import CircuitBreaker, FyersBroker


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

    def get_profile(self) -> Any:  # noqa: ANN401
        return self._answer("get_profile", None)


def broker_over(sdk: RecordingSdk) -> FyersBroker:
    """The real `FyersBroker` over `sdk`, with a breaker of its own (so no state leaks between
    examples)."""
    return FyersBroker(lambda: sdk, breaker=CircuitBreaker())


class RecordingSession:
    """A fake fyers_apiv3 SessionModel for the login flow. `reply` is what `generate_token`
    returns (or raises, when it is an exception); every constructor argument, the auth code it was
    given and the number of exchanges are recorded."""

    def __init__(self, reply: Any = None, **kwargs: str) -> None:  # noqa: ANN401 - raw SDK JSON
        self.kwargs = kwargs
        self.reply = reply
        self.auth_code: str | None = None
        self.exchanges = 0

    def generate_authcode(self) -> str:
        return f"https://login.example.test/?client_id={self.kwargs.get('client_id')}"

    def set_token(self, auth_code: str) -> None:
        self.auth_code = auth_code

    def generate_token(self) -> Mapping[str, Any]:
        self.exchanges += 1
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply
