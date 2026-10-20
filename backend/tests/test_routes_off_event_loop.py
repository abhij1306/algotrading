"""Routes that call blocking services must not block the event loop.

A blocking call made from an `async def` route stalls every other request and the
WebSocket stream until it returns. Each route below gets a fake service that sleeps,
and a trivial async endpoint must still answer while that request is in flight.
"""

import asyncio
import time
from collections.abc import Callable, Generator
from datetime import date, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.routers import backtest, market_dashboard, options, universe
from app.services.option_chain_service import OptionChainData, OptionLeg, OptionStrike

SLOW_SECONDS = 0.4
EXPIRY = date(2025, 2, 27)


def _slow(value: object) -> Callable[..., object]:
    def call(*_args: object, **_kwargs: object) -> object:
        time.sleep(SLOW_SECONDS)
        return value

    return call


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
    def __init__(self, sleep: bool) -> None:
        self.sleep = sleep

    def _wait(self) -> None:
        if self.sleep:
            time.sleep(SLOW_SECONDS)

    def get_option_chain(self, *_args: object, **_kwargs: object) -> OptionChainData:
        self._wait()
        return _chain()

    def get_expiries(self, _underlying: str) -> list[date]:
        self._wait()
        return [EXPIRY, date(2025, 3, 6)]

    def get_greeks(self, **_kwargs: object) -> dict[str, float]:
        self._wait()
        return {"delta": 0.5, "gamma": 0.001}


class FakeLive:
    def is_market_open(self) -> bool:
        return False

    async def get_status(self) -> dict[str, Any]:
        return {"market_status": "closed"}


class FakeUniverseService:
    def list_available_indices(self) -> list[dict[str, str]]:
        time.sleep(SLOW_SECONDS)
        return [{"index_code": "NIFTY50", "name": "Nifty 50", "description": "Top 50"}]


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
def slow_services(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(options, "option_chain_service", FakeOptionService(sleep=True))
    monkeypatch.setattr(backtest.backtest_phase1_service, "get_status", _slow({"data_ready": True}))
    monkeypatch.setattr(universe, "get_universe_service", FakeUniverseService)
    monkeypatch.setattr(market_dashboard, "live_market", FakeLive())
    monkeypatch.setattr(
        market_dashboard, "_fallback_index_quotes", _slow({"NIFTY50": {"price": 1}})
    )


async def _ping_answer_time_during(url: str) -> tuple[float, float]:
    """Seconds from sending `url` until a ping answered, and until `url` answered."""
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        started = time.perf_counter()
        slow = asyncio.create_task(client.get(url))
        await asyncio.sleep(0.05)  # let the slow request reach its blocking call
        ping = await client.get("/ping")
        ping_seconds = time.perf_counter() - started
        response = await slow
        slow_seconds = time.perf_counter() - started
    assert ping.json() == {"pong": "ok"}
    assert response.status_code == 200, response.text
    return ping_seconds, slow_seconds


@pytest.mark.usefixtures("slow_services")
@pytest.mark.parametrize(
    "url",
    [
        "/api/options/chain?symbol=nifty",
        "/api/options/expiries?symbol=nifty",
        "/api/options/atm?symbol=nifty",
        "/api/options/greeks?symbol=nifty&strike=22000&option_type=CE",
        "/api/backtest/status",
        "/api/universe/list",
        "/market/market-overview",
    ],
)
def test_slow_service_does_not_block_other_requests(url: str) -> None:
    ping_seconds, slow_seconds = asyncio.run(_ping_answer_time_during(url))

    assert slow_seconds >= SLOW_SECONDS
    assert ping_seconds < 0.25


@pytest.fixture
def options_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(options, "option_chain_service", FakeOptionService(sleep=False))
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
