"""Legacy helpers over the fake data socket, which lives in `tests/hat/fakes/data_socket.py` (K1).
Delete this module with the legacy suite (K3/K5)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from hat.fakes.data_socket import (
    FAST_RETRY,
    FakeDataSocket,
    FakeSocketFactory,
    make_live,
    run_inline,
)

from app.services.live.provider_session import ProviderSession
from app.services.live_market_service import LiveMarketService
from app.utils.clock import SYSTEM_CLOCK, Clock


def wait_until_backing_off(factory: FakeSocketFactory, timeout_s: float = 5.0) -> None:
    """Block until the runner's first attempt failed and released its socket (it then backs
    off). Fails the test if that does not happen in time."""
    deadline = time.monotonic() + timeout_s
    while not (factory.sockets and factory.sockets[0].closed):
        if time.monotonic() >= deadline:
            raise AssertionError("the connect runner never failed its first attempt")
        time.sleep(0.005)


def make_session(
    factory: FakeSocketFactory | None = None,
    *,
    desired: Callable[[], list[str]] = list,
    on_message: Callable[[dict[str, Any]], None] = lambda _m: None,
    token_source: Callable[[], str | None] = lambda: "CLIENT:tok",
    spawn: Callable[[Callable[[], None]], None] = run_inline,
) -> tuple[ProviderSession, FakeSocketFactory]:
    factory = factory or FakeSocketFactory()
    session = ProviderSession(
        socket_factory=factory,
        token_source=token_source,
        desired_symbols=desired,
        on_message=on_message,
        retry=FAST_RETRY,
        spawn=spawn,
    )
    return session, factory


def connected_live(clock: Clock = SYSTEM_CLOCK) -> tuple[LiveMarketService, FakeSocketFactory]:
    """make_live() with the provider session already connected (market hours not checked)."""
    live, factory = make_live(clock=clock)
    assert live.session.start() is True
    return live, factory


__all__ = [
    "FAST_RETRY",
    "FakeDataSocket",
    "FakeSocketFactory",
    "connected_live",
    "make_live",
    "make_session",
    "run_inline",
    "wait_until_backing_off",
]
