"""Regression tests for what is left of the legacy live market service after K3: it owns no
socket, reads ticks and subscriptions through HAT's feed (`app.hat.data.feed`), and degrades when
HAT is disabled. The socket, the session and the subscriptions are HAT's and are verified under
`tests/hat` (`properties/test_feed.py`). K5b deletes this suite with the service."""

import asyncio
import logging
import time
from typing import Any

import pytest

from app.hat.clock import FixedClock
from app.services import fyers_client as fyers_client_module
from app.services import live_market_service as lms
from app.utils.ws_manager import ConnectionManager

from fakes.market import MARKET_OPEN_INSTANT, FreezeMarket


def test_reset_fyers_client_drops_class_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fyers_client_module.FyersClient, "_instance", object())
    fyers_client_module.reset_fyers_client()
    assert fyers_client_module.FyersClient._instance is None


def test_flush_loop_survives_a_broadcast_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> tuple[list[dict[str, Any]], bool]:
        mgr = ConnectionManager()
        calls: list[dict[str, Any]] = []

        async def flaky_broadcast(msg: dict[str, Any]) -> None:
            calls.append(msg)
            if len(calls) == 1:
                raise RuntimeError("boom")

        monkeypatch.setattr(mgr, "broadcast", flaky_broadcast)
        monkeypatch.setattr(lms, "manager", mgr)
        monkeypatch.setattr(lms, "LOOP_ERROR_PAUSE_S", 0.01)
        live = lms.LiveMarketService()
        task = asyncio.create_task(live._flush_loop())
        live.tick_buffer = {"A": {"symbol": "A"}}
        await asyncio.sleep(0.3)
        live.tick_buffer = {"B": {"symbol": "B"}}
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return calls, True
        return calls, False

    calls, cancelled = asyncio.run(scenario())
    assert [c["data"][0]["symbol"] for c in calls] == ["A", "B"]
    assert cancelled is True


def _tick_at(ts: float) -> dict[str, Any]:
    return {"symbol": "SBIN", "ltp": 100.0, "exch_feed_time": ts}


@pytest.mark.parametrize(
    ("market_open", "age", "fresh"),
    [(True, 5.0, True), (True, 120.0, False), (False, 5.0, False)],
)
def test_get_fresh_tick_requires_open_market_and_recent_tick(
    monkeypatch: pytest.MonkeyPatch,
    freeze_market: FreezeMarket,
    market_open: bool,
    age: float,
    fresh: bool,
) -> None:

    monkeypatch.delenv("PAPER_TICK_MAX_AGE_SEC", raising=False)
    live = lms.LiveMarketService()
    freeze_market(market_open, live)
    live.latest_values = {"SBIN": _tick_at(time.time() - age), "TCS": {"ltp": 1.0}}

    assert (live.get_fresh_tick("SBIN") is not None) is fresh
    assert live.get_fresh_tick("TCS") is None  # no exchange time: stale
    assert live.get_fresh_tick("INFY") is None
    expected = {"SBIN"} if fresh else set()
    assert set(live.get_fresh_ticks(["SBIN", "TCS", "INFY"])) == expected


class FixedStatusLiveMarket:
    """A live_market whose connect always reports one status."""

    def __init__(self, status: str) -> None:
        self.status = status

    async def connect_async(self, **_kwargs: object) -> str:
        await asyncio.sleep(0)
        return self.status


def test_connect_route_and_startup_log_report_the_same_status_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(lms, "live_market", FixedStatusLiveMarket("token_invalid"))

    from app.main import _log_live_market_status
    from app.routers.websocket import connect_websocket

    assert asyncio.run(connect_websocket()) == {
        "status": "token_invalid",
        "message": "Live feed deferred because the Fyers token is invalid or expired",
    }

    with caplog.at_level(logging.INFO, logger="smarttrader.main"):
        _log_live_market_status("token_invalid")
    assert [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("[WARN] Live market")
    ] == [
        "[WARN] Live market: Live feed deferred because the Fyers token is invalid or expired "
        "(token_invalid)"
    ]


def test_connect_route_reports_a_generic_message_for_an_unknown_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lms, "live_market", FixedStatusLiveMarket("something_new"))

    from app.routers.websocket import connect_websocket

    assert asyncio.run(connect_websocket()) == {
        "status": "something_new",
        "message": "Live feed connection completed",
    }


def test_ticks_of_the_hat_feed_reach_the_legacy_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    from hat.fakes.data_socket import make_feed

    async def scenario() -> lms.LiveMarketService:
        monkeypatch.setattr(lms, "manager", ConnectionManager())
        feed, factory = make_feed(clock=FixedClock(MARKET_OPEN_INSTANT))
        feed.loop = asyncio.get_running_loop()
        live = lms.LiveMarketService()
        live.bind_feed(feed)
        assert feed.connect(explicit=True) == "started"
        factory.last.emit({"symbol": "NSE:SBIN-EQ", "ltp": 500.5, "v": 1000})
        await asyncio.sleep(0.05)
        assert lms.manager.registry is feed.registry  # one registry for HAT and the clients
        live.unbind_feed()
        factory.last.emit({"symbol": "NSE:TCS-EQ", "ltp": 1.0})
        await asyncio.sleep(0.05)
        return live

    live = asyncio.run(scenario())
    assert live.latest_values["SBIN"]["ltp"] == pytest.approx(500.5)
    assert live.tick_buffer["SBIN"]["volume"] == 1000
    assert "TCS" not in live.latest_values  # detached


def test_without_hat_the_legacy_feed_is_degraded_not_broken() -> None:
    live = lms.LiveMarketService()

    assert live.connect(prevalidated_token=True) == "hat_disabled"
    assert asyncio.run(live.connect_async(explicit=True)) == "hat_disabled"
    asyncio.run(live.subscribe(["SBIN"]))  # nothing to subscribe at
    asyncio.run(live.unsubscribe(["SBIN"]))
    live.disconnect()  # the socket is HAT's: nothing to close
    status = asyncio.run(live.get_status())
    assert status["fyers_connected"] is False
    assert status["provider_state"] == "hat_disabled"
    assert lms.connect_status_message("hat_disabled").startswith("Live feed unavailable")


def test_legacy_subscriptions_reach_hats_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    from hat.fakes.data_socket import make_feed

    monkeypatch.setattr(lms, "manager", ConnectionManager())
    feed, factory = make_feed(clock=FixedClock(MARKET_OPEN_INSTANT))
    live = lms.LiveMarketService()
    live.bind_feed(feed)
    assert feed.connect(explicit=True) == "started"

    asyncio.run(live.subscribe_api(["SBIN", "TCS"]))
    feed.registry.remove("api", ["TCS"])
    asyncio.run(live.unsubscribe(["TCS"]))

    assert factory.last.net_subscribed() == {"NSE:SBIN-EQ"}
