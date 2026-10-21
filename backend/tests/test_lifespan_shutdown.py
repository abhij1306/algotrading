"""App shutdown: every hook runs, and one failing hook does not skip the others."""

import logging

import pytest
from fastapi import FastAPI

from app import database
from app import main as app_main
from app.hat.execution import fyers_session


class _NoToken:
    def validate_token(self) -> bool:
        return False


class FakeEngine:
    def __init__(self, calls: list[str], fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail

    def dispose(self) -> None:
        self.calls.append("database_engine")
        if self.fail:
            raise OSError("pool already closed")


def install_fakes(
    monkeypatch: pytest.MonkeyPatch, calls: list[str], failing: str | None = None
) -> None:
    monkeypatch.setattr(app_main, "_ensure_database_schema", lambda: None)
    monkeypatch.setattr(fyers_session, "get_fyers_session", lambda: _NoToken())
    monkeypatch.setattr(database, "engine", FakeEngine(calls, failing == "database_engine"))


def shutdown_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("[WARN] Shutdown step")]


@pytest.mark.asyncio
async def test_shutdown_runs_every_hook_in_order(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []
    install_fakes(monkeypatch, calls)
    test_app = FastAPI()

    with caplog.at_level(logging.INFO):
        async with app_main.lifespan(test_app):
            assert calls == []

    assert calls == ["database_engine"]
    assert not hasattr(test_app.state, "deferred_startup")
    assert shutdown_warnings(caplog) == []
    assert "Shutdown complete" in [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failing", "message"),
    [
        ("database_engine", "[WARN] Shutdown step database_engine failed: pool already closed"),
    ],
)
async def test_a_failing_hook_does_not_skip_the_others(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing: str,
    message: str,
) -> None:
    calls: list[str] = []
    install_fakes(monkeypatch, calls, failing=failing)
    test_app = FastAPI()

    with caplog.at_level(logging.INFO):
        async with app_main.lifespan(test_app):
            pass

    assert calls == ["database_engine"]
    warnings = shutdown_warnings(caplog)
    assert [r.getMessage() for r in warnings] == [message]
    assert warnings[0].exc_info is None
    assert "Shutdown complete (1 step(s) failed)" in [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_hat_feed_is_stopped_only_when_it_exists() -> None:
    stopped: list[str] = []

    class Feed:
        async def stop(self) -> None:
            stopped.append("feed")

    app = FastAPI()
    await app_main._stop_hat_feed(app)  # HAT disabled: nothing to stop
    app.state.hat_feed = Feed()
    await app_main._stop_hat_feed(app)

    assert stopped == ["feed"]
    assert not hasattr(app.state, "hat_feed")
