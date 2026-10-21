"""Starting with the database down still runs the non-DB startup steps (the HAT feed start
included) and finishes the skipped database work once, in the background, when it is back."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI

from app import main as app_main
from app.exceptions import DatabaseUnavailableError
from app.hat.execution import fyers_session
from app.services.deferred_startup import (
    RECOVERED_MESSAGE,
    DeferredStartup,
    DeferredStartupStatus,
    startup_status,
)


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


class NoToken:
    def validate_token(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def no_fyers_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fyers_session, "get_fyers_session", lambda: NoToken())


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
async def test_db_down_startup_runs_the_other_steps_and_recovers_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Startup finds the DB down, so do the first two background retries; the third succeeds.
    work, calls = schema_work_down_for(3)
    monkeypatch.setattr(app_main, "_ensure_database_schema", work)
    monkeypatch.setattr(app_main, "DB_RETRY_INITIAL_DELAY_S", 0.001)
    monkeypatch.setattr(app_main, "DB_RETRY_MAX_DELAY_S", 0.004)
    test_app = FastAPI()

    with caplog.at_level(logging.INFO):
        async with app_main.lifespan(test_app):
            assert test_app.state.loop is asyncio.get_running_loop()
            assert test_app.state.fyers_token_valid is False
            deferred = test_app.state.deferred_startup
            await asyncio.wait_for(deferred.task, 1.0)
            assert deferred.status is DeferredStartupStatus.COMPLETE
            await asyncio.sleep(0.02)
            assert calls == ["down", "down", "down", "ok"]

    messages = [r.getMessage() for r in caplog.records]
    assert "[Startup] Completed: start_hat_feed" in messages
    assert recovery_lines(caplog) == [RECOVERED_MESSAGE]


@pytest.mark.asyncio
async def test_db_up_startup_starts_no_retry_task(monkeypatch: pytest.MonkeyPatch) -> None:
    work, calls = schema_work_down_for(0)
    monkeypatch.setattr(app_main, "_ensure_database_schema", work)
    test_app = FastAPI()

    async with app_main.lifespan(test_app):
        assert test_app.state.deferred_startup.task is None
        assert test_app.state.deferred_startup.status is DeferredStartupStatus.COMPLETE
        assert startup_status(test_app.state) is DeferredStartupStatus.COMPLETE

    assert calls == ["ok"]


@pytest.mark.asyncio
async def test_shutdown_cancels_the_pending_retry_task(monkeypatch: pytest.MonkeyPatch) -> None:
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
