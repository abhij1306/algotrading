"""Starting with the database down still runs the non-DB startup steps (live market connect
included) and finishes the skipped database work once, in the background, when it is back."""

import asyncio
import logging
from collections.abc import Callable, Generator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import main as app_main
from app.database import get_db
from app.exceptions import DatabaseUnavailableError
from app.main import app
from app.services import fyers_client, live_market_service
from app.services.deferred_startup import (
    RECOVERED_MESSAGE,
    DeferredStartup,
    DeferredStartupStatus,
    startup_status,
)


class FakeLiveMarket:
    def __init__(self) -> None:
        self.connect_calls: list[dict[str, Any]] = []

    def connect(
        self, loop: asyncio.AbstractEventLoop | None = None, prevalidated_token: bool | None = None
    ) -> str:
        self.connect_calls.append({"loop": loop, "prevalidated_token": prevalidated_token})
        return "market_closed"

    async def get_status(self) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"market_status": "CLOSED", "fyers_connected": False}


def schema_work_down_for(failures: int) -> tuple[Callable[[], None], list[str]]:
    """Schema work that finds the DB down for the first `failures` calls, then succeeds."""
    calls: list[str] = []

    def work() -> None:
        calls.append("down" if len(calls) < failures else "ok")
        if calls[-1] == "down":
            raise DatabaseUnavailableError()

    return work, calls


def recorded_sleep(delays: list[float]) -> Callable[[float], Any]:
    async def sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    return sleep


def recovery_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage() == RECOVERED_MESSAGE]


@pytest.fixture
def fake_live_market(monkeypatch: pytest.MonkeyPatch) -> FakeLiveMarket:
    fake = FakeLiveMarket()
    monkeypatch.setattr(live_market_service, "live_market", fake)
    monkeypatch.setattr(fyers_client, "get_fyers_client", lambda: None)
    return fake


@pytest.mark.asyncio
async def test_retries_with_backoff_until_the_database_answers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    work, calls = schema_work_down_for(5)
    delays: list[float] = []
    deferred = DeferredStartup(
        work, initial_delay=5.0, max_delay=60.0, sleep=recorded_sleep(delays)
    )

    with caplog.at_level(logging.DEBUG):
        deferred.start()
        assert deferred.status is DeferredStartupStatus.DEFERRED
        await asyncio.wait_for(deferred.task, 1.0)

    assert delays == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]
    assert calls == ["down"] * 5 + ["ok"]
    assert deferred.status is DeferredStartupStatus.COMPLETE
    assert recovery_lines(caplog) == [RECOVERED_MESSAGE]


@pytest.mark.asyncio
async def test_a_non_connection_error_stops_retrying_as_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []

    def broken_migration() -> None:
        calls.append("run")
        raise RuntimeError("column type mismatch")

    deferred = DeferredStartup(broken_migration, sleep=recorded_sleep([]))
    deferred.start()
    await asyncio.wait_for(deferred.task, 1.0)

    assert calls == ["run"]
    assert deferred.status is DeferredStartupStatus.FAILED
    assert recovery_lines(caplog) == []
    assert "column type mismatch" in caplog.text


@pytest.mark.asyncio
async def test_start_twice_runs_the_work_once() -> None:
    work, calls = schema_work_down_for(0)
    deferred = DeferredStartup(work, sleep=recorded_sleep([]))

    deferred.start()
    first_task = deferred.task
    deferred.start()
    await asyncio.wait_for(deferred.task, 1.0)

    assert deferred.task is first_task
    assert calls == ["ok"]


@pytest.mark.asyncio
async def test_db_down_startup_connects_live_market_and_recovers_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fake_live_market: FakeLiveMarket,
) -> None:
    # Startup finds the DB down, so do the first two background retries; the third succeeds.
    work, calls = schema_work_down_for(3)
    monkeypatch.setattr(app_main, "_ensure_database_schema", work)
    monkeypatch.setattr(app_main, "DB_RETRY_INITIAL_DELAY_S", 0.001)
    monkeypatch.setattr(app_main, "DB_RETRY_MAX_DELAY_S", 0.004)
    test_app = FastAPI()

    with caplog.at_level(logging.INFO):
        async with app_main.lifespan(test_app):
            assert fake_live_market.connect_calls == [
                {"loop": asyncio.get_running_loop(), "prevalidated_token": False}
            ]
            assert test_app.state.fyers_token_valid is False
            deferred = test_app.state.deferred_startup
            await asyncio.wait_for(deferred.task, 1.0)
            assert deferred.status is DeferredStartupStatus.COMPLETE
            await asyncio.sleep(0.02)
            assert calls == ["down", "down", "down", "ok"]

    messages = [r.getMessage() for r in caplog.records]
    assert "[Startup] Completed: connect_live_market" in messages
    assert "[Startup] Completed: load_index_universe" in messages
    assert recovery_lines(caplog) == [RECOVERED_MESSAGE]


@pytest.mark.asyncio
async def test_db_up_startup_starts_no_retry_task(
    monkeypatch: pytest.MonkeyPatch, fake_live_market: FakeLiveMarket
) -> None:
    work, calls = schema_work_down_for(0)
    monkeypatch.setattr(app_main, "_ensure_database_schema", work)
    test_app = FastAPI()

    async with app_main.lifespan(test_app):
        assert test_app.state.deferred_startup.task is None
        assert test_app.state.deferred_startup.status is DeferredStartupStatus.COMPLETE
        assert startup_status(test_app.state) is DeferredStartupStatus.COMPLETE

    assert calls == ["ok"]
    assert len(fake_live_market.connect_calls) == 1


@pytest.mark.asyncio
async def test_shutdown_cancels_the_pending_retry_task(
    monkeypatch: pytest.MonkeyPatch, fake_live_market: FakeLiveMarket
) -> None:
    work, calls = schema_work_down_for(100)
    monkeypatch.setattr(app_main, "_ensure_database_schema", work)
    monkeypatch.setattr(app_main, "DB_RETRY_INITIAL_DELAY_S", 30.0)
    test_app = FastAPI()

    async with app_main.lifespan(test_app):
        deferred = test_app.state.deferred_startup
        await asyncio.sleep(0)
        assert not deferred.task.done()

    assert deferred.task.cancelled()
    assert calls == ["down"]
    assert deferred.status is DeferredStartupStatus.DEFERRED
    assert not hasattr(test_app.state, "deferred_startup")


@pytest.fixture
def no_lifespan_client(db_session: Session) -> Generator[TestClient, None, None]:
    """A client on the test database that does not run the real lifespan."""

    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_status_endpoints_report_deferred_startup(
    no_lifespan_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_live_market: FakeLiveMarket,
) -> None:
    client = no_lifespan_client
    deferred = DeferredStartup(lambda: None)
    deferred.status = DeferredStartupStatus.DEFERRED
    monkeypatch.setattr(app.state, "deferred_startup", deferred, raising=False)

    health = client.get("/api/system/health")
    ws_status = client.get("/api/websocket/status")

    assert health.status_code == 200
    assert health.json() == {
        "status": "DEGRADED",
        "postgresql": "OK",
        "fyers": "DOWN",
        "last_update": None,
        "total_symbols": 0,
        "recommendations": [
            "Fyers API unavailable. Screener will use cached data only.",
            "No historical data found. Run canonical bhavcopy + phase1 pipeline.",
            "Database setup deferred at startup (database was down); it completes automatically.",
        ],
        "startup": "DEFERRED",
    }
    assert ws_status.json() == {
        "market_status": "CLOSED",
        "fyers_connected": False,
        "startup": "DEFERRED",
    }

    deferred.status = DeferredStartupStatus.COMPLETE

    assert client.get("/api/system/health").json()["startup"] == "COMPLETE"
    assert client.get("/api/websocket/status").json()["startup"] == "COMPLETE"
