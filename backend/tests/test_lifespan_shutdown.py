"""App shutdown: every hook runs, and one failing hook does not skip the others."""

import asyncio
import logging
from typing import Any

import pytest
from fastapi import FastAPI

from app import database
from app import main as app_main
from app.services import fyers_client, live_market_service
from app.services.live.provider_session import SessionState
from app.utils import ws_manager
from app.utils.ws_manager import ConnectionManager

from fakes.data_socket import connected_live


class FakeLiveMarket:
    def __init__(self, calls: list[str], fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail

    def connect(
        self, loop: asyncio.AbstractEventLoop | None = None, prevalidated_token: bool | None = None
    ) -> str:
        return "market_closed"

    async def shutdown(self) -> None:
        await asyncio.sleep(0)
        self.calls.append("live_market")
        if self.fail:
            raise ConnectionError("provider socket already closed")


class FakeManager:
    def __init__(self, calls: list[str], fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail

    async def close_all(self) -> int:
        await asyncio.sleep(0)
        self.calls.append("websocket_clients")
        if self.fail:
            raise RuntimeError("socket gone")
        return 0


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
    monkeypatch.setattr(fyers_client, "get_fyers_client", lambda: None)
    monkeypatch.setattr(
        live_market_service, "live_market", FakeLiveMarket(calls, failing == "live_market")
    )
    monkeypatch.setattr(ws_manager, "manager", FakeManager(calls, failing == "websocket_clients"))
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

    assert calls == ["live_market", "websocket_clients", "database_engine"]
    assert not hasattr(test_app.state, "deferred_startup")
    assert shutdown_warnings(caplog) == []
    assert "Shutdown complete" in [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failing", "message"),
    [
        ("live_market", "[WARN] Shutdown step live_market failed: provider socket already closed"),
        ("websocket_clients", "[WARN] Shutdown step websocket_clients failed: socket gone"),
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

    assert calls == ["live_market", "websocket_clients", "database_engine"]
    warnings = shutdown_warnings(caplog)
    assert [r.getMessage() for r in warnings] == [message]
    assert warnings[0].exc_info is None
    assert "Shutdown complete (1 step(s) failed)" in [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_live_market_shutdown_stops_its_loops_and_closes_the_provider() -> None:
    service, factory = connected_live()
    service.loop = asyncio.get_running_loop()
    service._ensure_background_tasks()
    monitor, flush = service.monitor_task, service.broadcast_task

    await service.shutdown()

    assert monitor is not None and monitor.cancelled()
    assert flush is not None and flush.cancelled()
    assert (service.monitor_task, service.broadcast_task, service.loop) == (None, None, None)
    assert factory.last.closed is True
    assert service.session.state is SessionState.IDLE
    # Not an explicit disconnect: a later startup may connect again.
    assert service._explicitly_disconnected is False


class FakeClientSocket:
    def __init__(self, fail: bool = False) -> None:
        self.closed_with: list[int] = []
        self.fail = fail

    async def accept(self) -> None:
        await asyncio.sleep(0)

    async def send_json(self, payload: dict[str, Any]) -> None:
        await asyncio.sleep(0)

    async def close(self, code: int = 1000) -> None:
        await asyncio.sleep(0)
        if self.fail:
            raise RuntimeError("already disconnected")
        self.closed_with.append(code)


@pytest.mark.asyncio
async def test_close_all_closes_every_client_and_stops_their_senders() -> None:
    manager = ConnectionManager()
    released: list[list[str]] = []

    async def on_released(symbols: list[str]) -> None:
        await asyncio.sleep(0)
        released.append(symbols)

    manager.on_symbols_released = on_released
    gone, alive = FakeClientSocket(fail=True), FakeClientSocket()
    for socket in (gone, alive):
        assert await manager.connect(socket)  # type: ignore[arg-type]
        await manager.subscribe(socket, ["SBIN"])  # type: ignore[arg-type]
    senders = [client.sender_task for client in manager.connections.values()]

    closed = await manager.close_all()

    assert closed == 2
    assert alive.closed_with == [1001]
    assert manager.connections == {}
    assert manager.registry.all_symbols() == set()
    assert all(task is not None and task.cancelled() for task in senders)
    assert released == []
