"""The Fyers quote parser and the gateway's quotes() on a fake SDK."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.services.fyers import quotes as quotes_module
from app.services.fyers.quotes import Quote, parse_quotes

from fakes.fyers_sdk import FakeFyersSdk, error_row, gateway, quote_row

SBIN_VALUES = {
    "ch": 12.5,
    "chp": 1.56,
    "lp": 812.5,
    "spread": 0.05,
    "ask": 812.55,
    "bid": 812.5,
    "open_price": 801.0,
    "high_price": 815.0,
    "low_price": 799.0,
    "prev_close_price": 800.0,
    "volume": 1234567,
    "short_name": "SBIN-EQ",
    "exchange": "NSE",
    "description": "NSE:SBIN-EQ",
    "original_name": "NSE:SBIN-EQ",
    "fyToken": "10100000003045",
    "tt": 1760927400,
}

SBIN = Quote(
    symbol="SBIN",
    ltp=Decimal("812.5"),
    change=Decimal("12.5"),
    change_pct=Decimal("1.5625"),
    open=Decimal("801.0"),
    high=Decimal("815.0"),
    low=Decimal("799.0"),
    prev_close=Decimal("800.0"),
    volume=1234567,
    timestamp=datetime(2025, 10, 20, 2, 30, tzinfo=UTC),
)


def _missing(symbol: str, **known: Any) -> Quote:  # noqa: ANN401 - Quote field values
    fields: dict[str, Any] = {
        "ltp": None,
        "change": None,
        "change_pct": None,
        "open": None,
        "high": None,
        "low": None,
        "prev_close": None,
        "volume": None,
        "timestamp": None,
    }
    return Quote(symbol=symbol, **{**fields, **known})


def _ok(*rows: object) -> dict[str, object]:
    return {"s": "ok", "code": 200, "d": list(rows)}


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        pytest.param(_ok(quote_row("NSE:SBIN-EQ", **SBIN_VALUES)), {"SBIN": SBIN}, id="ok"),
        pytest.param(
            _ok(quote_row("NSE:SBIN-EQ", **SBIN_VALUES), error_row("NSE:NOPE-EQ")),
            {"SBIN": SBIN},
            id="error-entry-skipped",
        ),
        pytest.param(
            _ok({"n": "NSE:NOPE-EQ", "s": "ok", "v": {"errmsg": "invalid symbol"}}),
            {},
            id="errmsg-without-status",
        ),
        pytest.param(
            _ok(quote_row("NSE:NIFTY50-INDEX", lp=25000.5)),
            {"NIFTY50": _missing("NIFTY50", ltp=Decimal("25000.5"))},
            id="missing-fields",
        ),
        pytest.param(
            _ok(quote_row("NSE:SBIN-EQ", lp=0, prev_close_price=800, volume=0, open_price=0)),
            {"SBIN": _missing("SBIN", prev_close=Decimal(800), volume=0)},
            id="zero-ltp-is-missing",
        ),
        pytest.param(
            _ok(
                quote_row(
                    "NSE:SBIN-EQ",
                    lp="812.50",
                    prev_close_price=" 800 ",
                    high_price="815",
                    volume="1234567",
                    tt="1760927400",
                )
            ),
            {
                "SBIN": _missing(
                    "SBIN",
                    ltp=Decimal("812.50"),
                    change=Decimal("12.50"),
                    change_pct=Decimal("1.5625"),
                    high=Decimal(815),
                    prev_close=Decimal(800),
                    volume=1234567,
                    timestamp=datetime(2025, 10, 20, 2, 30, tzinfo=UTC),
                )
            },
            id="string-numbers",
        ),
        pytest.param(
            _ok(
                quote_row(
                    "NSE:SBIN-EQ",
                    lp="abc",
                    prev_close_price=float("nan"),
                    open_price=True,
                    high_price=[1],
                    low_price=-5,
                    volume=12.5,
                    tt=-1,
                )
            ),
            {"SBIN": _missing("SBIN")},
            id="garbage-values",
        ),
        pytest.param(
            _ok(quote_row("NSE:SBIN-EQ", tt=10**20)),
            {"SBIN": _missing("SBIN")},
            id="out-of-range-timestamp",
        ),
        pytest.param(
            _ok(
                None,
                "NSE:SBIN-EQ",
                {"n": "", "v": {"lp": 1}},
                {"n": "NSE:SBIN-EQ"},
                {"n": "NSE:SBIN-EQ", "v": [1, 2]},
                {"n": 7, "v": {"lp": 1}},
                quote_row("NSE:TCS-EQ", lp=4000),
            ),
            {"TCS": _missing("TCS", ltp=Decimal(4000))},
            id="garbage-rows",
        ),
        pytest.param(
            {"s": "ok", "d": quote_row("NSE:TCS-EQ", lp=4000)},
            {"TCS": _missing("TCS", ltp=Decimal(4000))},
            id="single-row-d",
        ),
        pytest.param(
            {"s": "error", "code": -16, "message": "Could not authenticate the user"},
            {},
            id="error-reply",
        ),
        pytest.param({"s": "ok"}, {}, id="no-d"),
        pytest.param({"s": "ok", "d": "nope"}, {}, id="d-not-a-list"),
        pytest.param(None, {}, id="none"),
        pytest.param("garbage", {}, id="not-a-mapping"),
    ],
)
def test_parse_quotes(response: object, expected: dict[str, Quote]) -> None:
    assert parse_quotes(response) == expected


def test_a_row_that_raises_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    class ExplodingSymbolMaster:
        def to_db(self, symbol: str) -> str:
            if symbol == "NSE:BAD-EQ":
                raise ValueError("unmapped")
            return symbol.removeprefix("NSE:").removesuffix("-EQ")

    monkeypatch.setattr(quotes_module, "symbol_master", ExplodingSymbolMaster())

    quotes = parse_quotes(_ok(quote_row("NSE:BAD-EQ", lp=1), quote_row("NSE:TCS-EQ", lp=4000)))

    assert quotes == {"TCS": _missing("TCS", ltp=Decimal(4000))}


def test_quotes_converts_symbols_and_keys_by_db_symbol() -> None:
    sdk = FakeFyersSdk(
        {
            "NSE:SBIN-EQ": quote_row("NSE:SBIN-EQ", **SBIN_VALUES),
            "NSE:NIFTY50-INDEX": quote_row("NSE:NIFTY50-INDEX", lp=25000.5),
        }
    )

    quotes = gateway(sdk).quotes(["SBIN", "NIFTY50", "NOPE", "SBIN", ""])

    assert sdk.requests == [["NSE:SBIN-EQ", "NSE:NIFTY50-INDEX", "NSE:NOPE-EQ"]]
    assert quotes == {"SBIN": SBIN, "NIFTY50": _missing("NIFTY50", ltp=Decimal("25000.5"))}


def test_quotes_batches_fifty_symbols_per_request() -> None:
    symbols = [f"S{i}" for i in range(120)]
    sdk = FakeFyersSdk({f"NSE:S{i}-EQ": quote_row(f"NSE:S{i}-EQ", lp=i + 1) for i in range(120)})

    quotes = gateway(sdk).quotes(symbols)

    assert [len(batch) for batch in sdk.requests] == [50, 50, 20]
    assert sdk.requests[2][-1] == "NSE:S119-EQ"
    assert len(quotes) == 120
    assert quotes["S119"].ltp == Decimal(120)


def test_quotes_survives_a_failed_request() -> None:
    sdk = FakeFyersSdk(fail=RuntimeError("socket reset"))

    assert gateway(sdk).quotes(["SBIN"]) == {}
    assert sdk.requests == [["NSE:SBIN-EQ"]]


def test_quotes_without_a_connected_sdk_or_symbols_is_empty() -> None:
    sdk = FakeFyersSdk()

    assert gateway(None).quotes(["SBIN"]) == {}
    assert gateway(sdk).quotes([]) == {}
    assert sdk.requests == []
