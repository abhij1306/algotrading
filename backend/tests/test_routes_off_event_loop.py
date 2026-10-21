"""Response shapes of the options routes, against a fake option service.

The timing test that checked a slow service did not block the event loop was deleted: it
asserted wall-clock bounds and failed under CI load (legacy debt, D59).
"""

from collections.abc import Generator
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.routers import backtest, market_dashboard, options, universe
from app.services.option_chain_service import OptionChainData, OptionLeg, OptionStrike

EXPIRY = date(2025, 2, 27)


def _chain() -> OptionChainData:
    call = OptionLeg(
        symbol="NIFTY25FEB22000CE",
        fyers_symbol="NSE:NIFTY25FEB22000CE",
        strike=22000.0,
        expiry=EXPIRY,
        option_type="CE",
        ltp=120.5,
        oi=1000,
        greeks={"delta": 0.5},
    )
    return OptionChainData(
        underlying="NIFTY",
        spot_price=22010.0,
        expiry=EXPIRY,
        strikes=[OptionStrike(strike_price=22000.0, call=call), OptionStrike(strike_price=22100.0)],
        timestamp=datetime(2025, 2, 20, 10, 0, 0),
    )


class FakeOptionService:
    def get_option_chain(self, *_args: object, **_kwargs: object) -> OptionChainData:
        return _chain()

    def get_expiries(self, _underlying: str) -> list[date]:
        return [EXPIRY, date(2025, 3, 6)]

    def get_greeks(self, **_kwargs: object) -> dict[str, float]:
        return {"delta": 0.5, "gamma": 0.001}


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(options.router)
    app.include_router(backtest.router, prefix="/api/backtest")
    app.include_router(universe.router, prefix="/api/universe")
    app.include_router(market_dashboard.router)

    def no_db() -> Generator[None, None, None]:
        yield None

    app.dependency_overrides[get_db] = no_db

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"pong": "ok"}

    return app


@pytest.fixture
def options_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(options, "option_chain_service", FakeOptionService())
    return TestClient(_app())


def test_option_chain_response(options_client: TestClient) -> None:
    response = options_client.get("/api/options/chain", params={"symbol": "nifty"})

    assert response.status_code == 200
    assert response.json() == {
        "underlying": "NIFTY",
        "spot_price": 22010.0,
        "expiry": "2025-02-27",
        "strikes": [
            {
                "strike_price": 22000.0,
                "ce": {
                    "symbol": "NIFTY25FEB22000CE",
                    "fyers_symbol": "NSE:NIFTY25FEB22000CE",
                    "strike": 22000.0,
                    "expiry": "2025-02-27",
                    "option_type": "CE",
                    "ltp": 120.5,
                    "bid": 0.0,
                    "ask": 0.0,
                    "volume": 0,
                    "oi": 1000,
                    "iv": 0.0,
                    "greeks": {"delta": 0.5},
                    "change": 0.0,
                    "change_pct": 0.0,
                    "prev_close": 0.0,
                },
                "pe": None,
            },
            {"strike_price": 22100.0, "ce": None, "pe": None},
        ],
        "atm_strike": 22000.0,
        "timestamp": "2025-02-20T10:00:00",
    }


def test_expiries_response(options_client: TestClient) -> None:
    response = options_client.get("/api/options/expiries", params={"symbol": "nifty"})

    assert response.status_code == 200
    assert response.json() == {"underlying": "NIFTY", "expiries": ["2025-02-27", "2025-03-06"]}


def test_atm_response(options_client: TestClient) -> None:
    response = options_client.get("/api/options/atm", params={"symbol": "nifty"})

    assert response.status_code == 200
    assert response.json() == {"underlying": "NIFTY", "spot_price": 22010.0, "atm_strike": 22000.0}


def test_greeks_response(options_client: TestClient) -> None:
    response = options_client.get(
        "/api/options/greeks", params={"symbol": "nifty", "strike": 22000, "option_type": "CE"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "underlying": "NIFTY",
        "strike": 22000.0,
        "option_type": "CE",
        "expiry": "2025-02-27",
        "spot_price": 22010.0,
        "greeks": {"delta": 0.5, "gamma": 0.001},
    }


def test_option_chain_missing_is_404(
    options_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(options.option_chain_service, "get_option_chain", lambda **_: None)

    response = options_client.get("/api/options/chain", params={"symbol": "nifty"})

    assert response.status_code == 404
    assert response.json() == {
        "detail": "Option chain not available for nifty. Market may be closed or symbol invalid."
    }
