"""GET /api/portfolio/funds: parsing of the Fyers funds reply, failing closed (503) when
the broker is missing or failing."""

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.routers import portfolio
from app.services import fyers_client as fyers_client_module


class FakeFunds:
    def __init__(self, reply: object) -> None:
        self.reply = reply

    def funds(self) -> object:
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def use_client(monkeypatch: pytest.MonkeyPatch, client: object) -> None:
    monkeypatch.setattr(fyers_client_module, "get_fyers_client", lambda: client)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            {"available": 700, "used": 300, "total": 1000},
            {"available": 700.0, "used": 300.0, "total": 1000.0, "buyingPower": 700.0},
        ),
        (
            {"available": 700, "used": 300},
            {"available": 700.0, "used": 300.0, "total": 1000.0, "buyingPower": 700.0},
        ),
        (
            {
                "available": 1,
                "fund_limit": [
                    {"title": "Total Balance", "equityAmount": 50000.0},
                    {"title": "Available Balance", "equityAmount": 42000.0},
                    {"title": "Utilized Amount", "equityAmount": 8000.0},
                    {"title": "Other", "equityAmount": 5.0},
                ],
            },
            {"available": 42000.0, "used": 8000.0, "total": 50000.0, "buyingPower": 42000.0},
        ),
        ({"fund_limit": "n/a"}, {"available": 0.0, "used": 0.0, "total": 0.0, "buyingPower": 0.0}),
        (
            # One malformed or missing amount must not zero the other balances.
            {
                "fund_limit": [
                    {"title": "Total Balance", "equityAmount": 50000.0},
                    {"title": "Available Balance", "equityAmount": None},
                    {"title": "Utilized Amount", "equityAmount": "n/a"},
                    "junk",
                ],
                "available": 41000,
            },
            {"available": 41000.0, "used": 0.0, "total": 50000.0, "buyingPower": 41000.0},
        ),
        (
            {"available": "nan", "used": "12.5", "total": None},
            {"available": 0.0, "used": 12.5, "total": 12.5, "buyingPower": 0.0},
        ),
    ],
)
def test_funds_reply_is_mapped(
    monkeypatch: pytest.MonkeyPatch, reply: object, expected: dict[str, Any]
) -> None:
    use_client(monkeypatch, SimpleNamespace(fyers=FakeFunds(reply)))

    assert portfolio.get_portfolio_funds(db=None) == expected


@pytest.mark.parametrize(
    "client",
    [
        None,
        SimpleNamespace(fyers=None),
        SimpleNamespace(fyers=FakeFunds(RuntimeError("broker down"))),
        SimpleNamespace(fyers=FakeFunds(["not", "a", "dict"])),
        SimpleNamespace(fyers=FakeFunds({"s": "error", "message": "token expired"})),
    ],
)
def test_funds_fail_closed_when_broker_missing_or_failing(
    monkeypatch: pytest.MonkeyPatch, client: object
) -> None:
    use_client(monkeypatch, client)

    with pytest.raises(HTTPException) as raised:
        portfolio.get_portfolio_funds(db=None)

    assert raised.value.status_code == 503
    assert raised.value.detail == portfolio.FUNDS_UNAVAILABLE  # no broker error text
