"""Characterization tests for synthetic option pricing and option-chain parsing,
pinned ahead of a complexity refactor."""

from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.services.option_chain_service import OptionChainService, OptionLeg, OptionStrike
from app.strategies.black_scholes import price_synthetic_option

HIST = pd.DataFrame({"close": [100 + np.sin(i / 3) * 3 for i in range(40)]})


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"underlying_price": 22000.0}, 256.8204884408021),
        (
            {
                "underlying_price": 22000.0,
                "option_type": "PE",
                "strike": 22100.0,
                "days_to_expiry": 5,
            },
            248.51402240571943,
        ),
        (
            {
                "underlying_price": 500.0,
                "option_type": "CALL",
                "strike": 480.0,
                "days_to_expiry": 10,
                "volatility": 0.3,
                "apply_dividend_adjustment": False,
            },
            23.488400622257018,
        ),
        (
            {
                "underlying_price": 100.0,
                "option_type": "PUT",
                "strike": 100.0,
                "days_to_expiry": 7,
                "historical_data": HIST,
            },
            3.2506630882775145,
        ),
        (
            {
                "underlying_price": 100.0,
                "strike": 100.0,
                "days_to_expiry": 7,
                "historical_data": HIST.head(10),
            },
            1.1673658565491036,
        ),
        (
            {
                "underlying_price": 22000.0,
                "strike": 22000.0,
                "current_time": datetime(2025, 1, 6, 10, 0),
            },
            424.6111709637251,
        ),
        (
            {"underlying_price": 100.0, "strike": 500.0, "days_to_expiry": 1, "volatility": 0.01},
            1.0,
        ),
        (
            {
                "underlying_price": 500.0,
                "option_type": "PE",
                "strike": 100.0,
                "days_to_expiry": 1,
                "volatility": 0.01,
            },
            1.0,
        ),
    ],
)
def test_price_synthetic_option(kwargs: dict[str, Any], expected: float) -> None:
    assert price_synthetic_option(**kwargs) == pytest.approx(expected, rel=1e-12)


def test_extract_expiries_merges_new_and_legacy_schemas() -> None:
    raw = {
        "expiryData": [
            {"date": "27-02-2025", "expiry": "1740650400"},
            {"date": "", "expiry": "1741255200"},
            "junk",
            {"date": "27-02-2025"},
        ],
        "22000": {"2025-03-27": {}, "06MAR25": {}},
        "other": "x",
    }

    expiries = OptionChainService()._extract_expiries(raw)

    assert expiries == [date(2025, 2, 27), date(2025, 3, 6), date(2025, 3, 27)]
    assert OptionChainService()._extract_expiries({"expiryData": "nope"}) == []


EXPIRY = date(2025, 2, 27)


def leg(symbol: str, strike: float, option_type: str, **values: float) -> OptionLeg:
    return OptionLeg(
        symbol=symbol,
        fyers_symbol=symbol,
        strike=strike,
        expiry=EXPIRY,
        option_type=option_type,
        **values,
    )


def test_parse_strikes_from_flat_options_chain() -> None:
    raw = {
        "optionsChain": [
            {"strike_price": 22000, "option_type": "CE", "ltp": 120.5, "oi": 10, "volume": 5},
            {"strike_price": "22000", "option_type": "pe", "lp": 80.0, "ch": -2.0},
            {"strike_price": 22100, "option_type": "CE", "ltp": 70.0, "bid": 69.5, "ask": 70.5},
            {"strike_price": None, "option_type": "CE"},
            {"strike_price": 22200, "option_type": "FUT"},
            "junk",
        ]
    }

    strikes = OptionChainService()._parse_strikes(raw, EXPIRY, 22050.0, include_greeks=False)

    assert strikes == [
        OptionStrike(
            strike_price=22000.0,
            call=leg("", 22000.0, "CE", ltp=120.5, oi=10, volume=5, prev_close=120.5),
            put=leg("", 22000.0, "PE", ltp=80.0, change=-2.0, prev_close=82.0),
        ),
        OptionStrike(
            strike_price=22100.0,
            call=leg("", 22100.0, "CE", ltp=70.0, bid=69.5, ask=70.5, prev_close=70.0),
        ),
    ]


def test_parse_strikes_from_legacy_per_strike_schema() -> None:
    raw = {
        "22000": {"2025-02-27": {"CE": {"ltp": 10.0}, "PE": {}}},
        "22100": {"2025-02-27": {"PE": {"ltp": 30.0, "chp": 1.5}}},
        "22200": {"2025-03-06": {"CE": {"ltp": 5.0}}},
        "22300": "bad",
        "spot": {"2025-02-27": {"CE": {"ltp": 1.0}}},
    }

    strikes = OptionChainService()._parse_strikes(raw, EXPIRY, 22050.0, include_greeks=False)

    assert strikes == [
        OptionStrike(strike_price=22000.0, call=leg("", 22000.0, "CE", ltp=10.0, prev_close=10.0)),
        OptionStrike(
            strike_price=22100.0,
            put=leg("", 22100.0, "PE", ltp=30.0, change_pct=1.5, prev_close=30.0),
        ),
    ]


# --- flat optionsChain rows belong to one expiry (PR #34 review) ------------------

NEAR, FAR = date(2025, 2, 27), date(2025, 3, 6)
EXPIRY_DATA = [
    {"date": "27-02-2025", "expiry": "1740650400"},
    {"date": "06-03-2025", "expiry": "1741255200"},
]


def test_flat_rows_of_another_expiry_are_not_relabelled() -> None:
    raw = {"optionsChain": [{"strike_price": 22000, "option_type": "CE", "ltp": 1.0}]}
    service = OptionChainService()

    assert service._parse_strikes(raw, FAR, 22000.0, False, rows_expiry=NEAR) == []
    assert len(service._parse_strikes(raw, NEAR, 22000.0, False, rows_expiry=NEAR)) == 1


class FakeFyers:
    """optionchain(): the nearest expiry by default, the named one with a timestamp."""

    def __init__(self, dated_ok: bool = True) -> None:
        self.requests: list[dict[str, Any]] = []
        self.dated_ok = dated_ok

    def quotes(self, _data: dict[str, Any]) -> dict[str, Any]:
        return {"s": "ok", "d": [{"v": {"lp": 22000.0}}]}

    def optionchain(self, data: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(data)
        far = data.get("timestamp") == "1741255200"
        if far and not self.dated_ok:
            return {"s": "error", "message": "bad timestamp"}
        ltp = 99.0 if far else 11.0
        row = {"strike_price": 22000, "option_type": "CE", "ltp": ltp}
        return {"s": "ok", "data": {"expiryData": EXPIRY_DATA, "optionsChain": [row]}}


def _service(monkeypatch: pytest.MonkeyPatch, fyers: FakeFyers) -> OptionChainService:
    service = OptionChainService()
    monkeypatch.setattr(service, "_get_fyers_client", lambda: SimpleNamespace(fyers=fyers))
    return service


def test_requested_expiry_is_fetched_by_its_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    fyers = FakeFyers()

    chain = _service(monkeypatch, fyers).get_option_chain("NIFTY", expiry=FAR, strike_count=2)

    assert chain is not None
    assert chain.expiry == FAR
    assert [s.call.ltp for s in chain.strikes] == [99.0]
    assert fyers.requests[-1]["timestamp"] == "1741255200"


def test_nearest_expiry_needs_no_second_request(monkeypatch: pytest.MonkeyPatch) -> None:
    fyers = FakeFyers()

    chain = _service(monkeypatch, fyers).get_option_chain("NIFTY", expiry=None, strike_count=2)

    assert chain is not None
    assert chain.expiry == NEAR
    assert [s.call.ltp for s in chain.strikes] == [11.0]
    assert all("timestamp" not in r for r in fyers.requests)


def test_failed_dated_request_returns_no_strikes_rather_than_wrong_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = _service(monkeypatch, FakeFyers(dated_ok=False)).get_option_chain(
        "NIFTY", expiry=FAR, strike_count=2
    )

    assert chain is not None
    assert chain.expiry == FAR
    assert chain.strikes == []
