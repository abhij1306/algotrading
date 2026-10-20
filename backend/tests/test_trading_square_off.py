"""Live square-off error paths must not return broker or exception text (trading rule 10)."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import HTTPException

from app.routers import trading

from fakes.broker import FakeBroker

SECRET = "token=abc123 internal broker detail"


CLIENT_ORDER_ID = "5b0e4c2a-7d1f-4e3b-9a6c-2f8d1e0b7c45"


class FakeDb:
    rolled_back = False

    def rollback(self) -> None:
        self.rolled_back = True


@pytest.mark.parametrize(
    ("route", "request_model", "detail"),
    [
        (
            trading.square_position,
            trading.SquarePositionRequest(
                symbol="SBIN", mode="LIVE", client_order_id=CLIENT_ORDER_ID
            ),
            "Error squaring position",
        ),
        (
            trading.exit_position,
            trading.ExitPositionRequest(symbol="SBIN", client_order_id=CLIENT_ORDER_ID),
            "Error exiting position",
        ),
    ],
)
def test_live_square_and_exit_server_errors_are_generic(
    monkeypatch: pytest.MonkeyPatch,
    route: Callable[[Any, Any], object],
    request_model: object,
    detail: str,
) -> None:
    def explode(**_kwargs: object) -> dict[str, Any]:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(trading.live_exit_service, "exit_positions", explode)
    db = FakeDb()

    with pytest.raises(HTTPException) as raised:
        route(request_model, db)

    assert raised.value.status_code == 500
    assert raised.value.detail == detail
    assert db.rolled_back is True


@pytest.mark.parametrize(
    ("route", "detail"),
    [
        (trading.get_funds, "Error fetching funds"),
        (trading.get_tradebook, "Error fetching tradebook"),
    ],
)
def test_broker_route_errors_do_not_leak_exception_text(
    fake_broker: FakeBroker, route: Callable[[], object], detail: str
) -> None:
    fake_broker.available = RuntimeError(SECRET)
    fake_broker.tradebook = RuntimeError(SECRET)

    with pytest.raises(HTTPException) as raised:
        route()

    assert raised.value.status_code == 500
    assert raised.value.detail == detail
