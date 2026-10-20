"""Market routes and data_fetcher read live quotes through the gateway (fake SDK underneath)."""

import pytest

from app import data_fetcher
from app.routers import market
from app.services import fyers_client
from app.services import universe as universe_module

from fakes.fyers_sdk import FakeFyersSdk, gateway, quote_row


def _use_sdk(monkeypatch: pytest.MonkeyPatch, rows: dict[str, dict]) -> FakeFyersSdk:
    sdk = FakeFyersSdk({row["n"]: row for row in rows.values()})
    client = gateway(sdk)
    monkeypatch.setattr(fyers_client, "get_fyers_client", lambda: client)
    monkeypatch.setattr(data_fetcher, "get_fyers_client", lambda: client)
    return sdk


def _row(name: str, ltp: float, prev_close: float, **values: float) -> dict:
    return quote_row(name, lp=ltp, prev_close_price=prev_close, **values)


def test_market_indices(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = _use_sdk(
        monkeypatch,
        {
            "n": _row("NSE:NIFTY50-INDEX", 25250.0, 25000.0),
            "s": _row("BSE:SENSEX-INDEX", 82000.0, 0),  # no previous close: left out
            "b": _row("NSE:NIFTYBANK-INDEX", 0, 56000.0),  # no last price: left out
        },
    )

    assert market.get_market_indices() == [
        {
            "name": "NIFTY 50",
            "symbol": "NIFTY50",
            "value": 25250.0,
            "change": 250.0,
            "changePercent": 1.0,
        }
    ]
    assert sdk.requests == [
        ["NSE:NIFTY50-INDEX", "BSE:SENSEX-INDEX", "NSE:NIFTYBANK-INDEX", "NSE:NIFTYIT-INDEX"]
    ]


def test_market_indices_without_a_client_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fyers_client, "get_fyers_client", lambda: gateway(None))

    assert market.get_market_indices() == []


def test_live_quotes_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_sdk(
        monkeypatch,
        {
            "sbin": _row(
                "NSE:SBIN-EQ",
                812.5,
                800.0,
                volume=1000,
                high_price=815.0,
                low_price=799.0,
                open_price=801.0,
            ),
            "tcs": quote_row("NSE:TCS-EQ", lp=4000.0),
            "infy": _row("NSE:INFY-EQ", 0, 1500.0),
        },
    )

    assert market.get_live_quotes("SBIN, TCS,INFY,", db=None) == {
        "SBIN": {
            "ltp": 812.5,
            "volume": 1000,
            "high": 815.0,
            "low": 799.0,
            "open": 801.0,
            "prev_close": 800.0,
            "change_pct": 1.56,
        },
        "TCS": {
            "ltp": 4000.0,
            "volume": 0,
            "high": 0.0,
            "low": 0.0,
            "open": 0.0,
            "prev_close": 4000.0,
            "change_pct": 0.0,
        },
    }


def test_top_movers(monkeypatch: pytest.MonkeyPatch) -> None:
    symbols = ["AAA", "BBB", "CCC", "DDD"]
    monkeypatch.setattr(
        universe_module,
        "get_universe_service",
        lambda: type("U", (), {"get_symbols": staticmethod(lambda _index: symbols)})(),
    )
    _use_sdk(
        monkeypatch,
        {
            "a": _row("NSE:AAA-EQ", 110.0, 100.0),
            "b": _row("NSE:BBB-EQ", 95.0, 100.0),
            "c": _row("NSE:CCC-EQ", 0, 100.0),
            "d": quote_row("NSE:DDD-EQ", lp=50.456),
        },
    )

    assert market.get_top_movers(limit=2) == {
        "gainers": [
            {"symbol": "AAA", "name": "AAA", "price": 110.0, "changePercent": 10.0},
            {"symbol": "DDD", "name": "DDD", "price": 50.46, "changePercent": 0.0},
        ],
        "losers": [
            {"symbol": "BBB", "name": "BBB", "price": 95.0, "changePercent": -5.0},
            {"symbol": "DDD", "name": "DDD", "price": 50.46, "changePercent": 0.0},
        ],
    }


def test_top_movers_rank_by_the_unrounded_change(monkeypatch: pytest.MonkeyPatch) -> None:
    symbols = ["LOW", "HIGH"]
    monkeypatch.setattr(
        universe_module,
        "get_universe_service",
        lambda: type("U", (), {"get_symbols": staticmethod(lambda _index: symbols)})(),
    )
    _use_sdk(
        monkeypatch,
        {
            "low": _row("NSE:LOW-EQ", 100.001, 100.0),  # +0.001%
            "high": _row("NSE:HIGH-EQ", 100.004, 100.0),  # +0.004%, also rounds to 0.0
        },
    )

    result = market.get_top_movers(limit=1)

    assert result["gainers"] == [
        {"symbol": "HIGH", "name": "HIGH", "price": 100.0, "changePercent": 0.0}
    ]
    assert result["losers"] == [
        {"symbol": "LOW", "name": "LOW", "price": 100.0, "changePercent": 0.0}
    ]


def test_sector_performance(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_sdk(
        monkeypatch,
        {
            "bank": _row("NSE:NIFTYBANK-INDEX", 56123.456, 56000.0),
            "it": quote_row("NSE:NIFTYIT-INDEX", lp=35000.0),
            "pharma": _row("NSE:NIFTYPHARMA-INDEX", 0, 22000.0),
        },
    )

    assert market.get_sector_performance() == [
        {"name": "Banking", "symbol": "BANKNIFTY", "value": 56123.46, "changePercent": 0.22},
        {"name": "IT", "symbol": "NIFTYIT", "value": 35000.0, "changePercent": 0.0},
    ]


def test_fetch_fyers_quotes_keeps_the_legacy_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_fetcher, "fyers_available", lambda: True)
    monkeypatch.setattr(data_fetcher, "_quotes_cache", {})
    sdk = _use_sdk(
        monkeypatch,
        {
            "sbin": _row("NSE:SBIN-EQ", 812.5, 800.0, volume=1000, high_price=815.0),
            "tcs": _row("NSE:TCS-EQ", 0, 4000.0),
        },
    )

    first = data_fetcher.fetch_fyers_quotes(["SBIN", "TCS", "NOPE"])
    again = data_fetcher.fetch_fyers_quotes(["SBIN"])

    assert first == {
        "SBIN": {
            "ltp": 812.5,
            "volume": 1000,
            "high": 815.0,
            "low": 0,
            "open": 0,
            "prev_close": 800.0,
        },
        "TCS": {"ltp": 0, "volume": 0, "high": 0, "low": 0, "open": 0, "prev_close": 4000.0},
    }
    assert again == {"SBIN": first["SBIN"]}
    assert sdk.requests == [["NSE:SBIN-EQ", "NSE:TCS-EQ", "NSE:NOPE-EQ"]]
