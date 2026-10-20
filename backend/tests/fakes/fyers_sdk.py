"""A fake fyers_apiv3 FyersModel for the quotes endpoint, and a gateway built on it."""

from __future__ import annotations

from typing import Any

from app.services.fyers_client import FyersClient


def quote_row(name: str, **values: Any) -> dict[str, Any]:  # noqa: ANN401 - raw SDK JSON
    """One ``d`` entry shaped like a Fyers quotes reply row."""
    return {"n": name, "s": "ok", "v": {"symbol": name, **values}}


def error_row(name: str) -> dict[str, Any]:
    """The ``d`` entry Fyers sends for a symbol it does not know."""
    return {
        "n": name,
        "s": "error",
        "v": {"s": "error", "code": -300, "errmsg": "Please provide a valid symbol"},
    }


class FakeFyersSdk:
    """Answers ``quotes`` from a table of rows by Fyers symbol, recording every request."""

    def __init__(
        self, rows: dict[str, dict[str, Any]] | None = None, fail: Exception | None = None
    ) -> None:
        self.rows = rows or {}
        self.fail = fail
        self.requests: list[list[str]] = []

    def quotes(self, data: dict[str, str]) -> dict[str, Any]:
        symbols = data["symbols"].split(",")
        self.requests.append(symbols)
        if self.fail is not None:
            raise self.fail
        return {"s": "ok", "code": 200, "d": [self.rows.get(s, error_row(s)) for s in symbols]}


def gateway(sdk: FakeFyersSdk | None) -> FyersClient:
    """A FyersClient on ``sdk`` that is not the process singleton and never reads disk."""
    client = object.__new__(FyersClient)
    client.fyers = sdk
    client.token_expires_at = None
    return client
