"""ProviderSession on a fake DataSocket: connect, replay, drop, reconnect, close and tokens."""

import logging
import threading
from typing import Any

import pytest

from app.services.live.provider_session import ProviderSession, RetryPolicy, SessionState

from fakes.data_socket import FakeSocketFactory, make_session, wait_until_backing_off


def started(session: ProviderSession) -> None:
    assert session.start() is True


@pytest.mark.parametrize(
    ("wanted", "later", "expected"),
    [
        ([], ["NSE:SBIN-EQ"], ["NSE:SBIN-EQ"]),
        (["NSE:TCS-EQ"], ["NSE:SBIN-EQ"], ["NSE:TCS-EQ", "NSE:SBIN-EQ"]),
        # 30 symbols go out in batches of 25; duplicates and blanks are dropped.
        (
            [],
            [f"NSE:S{i:02d}-EQ" for i in range(30)] + ["NSE:S00-EQ", ""],
            [f"NSE:S{i:02d}-EQ" for i in range(30)],
        ),
    ],
)
def test_connect_then_subscribe(wanted: list[str], later: list[str], expected: list[str]) -> None:
    session, factory = make_session(desired=lambda: wanted)
    assert session.subscribe(later) is False  # nothing to send on before a connect

    started(session)

    assert session.state is SessionState.CONNECTED
    assert session.is_connected() is True
    assert session.subscribe(later) is True
    assert factory.last.subscribed == expected
    assert factory.last.connect_calls == 1


@pytest.mark.parametrize("fires_on_connect", [True, False])
def test_first_connect_replays_once_whether_or_not_the_sdk_fires_on_connect(
    fires_on_connect: bool,
) -> None:
    session, factory = make_session(
        FakeSocketFactory(fires_on_connect=fires_on_connect), desired=lambda: ["NSE:SBIN-EQ"]
    )

    started(session)

    assert session.state is SessionState.CONNECTED
    assert factory.last.subscribed == ["NSE:SBIN-EQ"]


@pytest.mark.parametrize("how", ["sdk_auto_reconnect", "new_socket"])
def test_a_reconnect_replays_the_subscriptions(how: str) -> None:
    wanted = ["NSE:SBIN-EQ"]
    session, factory = make_session(desired=lambda: wanted)
    started(session)
    first = factory.last
    wanted.append("NSE:TCS-EQ")  # subscribed while connected (sent directly)
    session.subscribe(["NSE:TCS-EQ"])

    first.drop()
    assert session.state is SessionState.DROPPED
    if how == "sdk_auto_reconnect":
        first.sdk_reconnect()
        socket = first
    else:
        started(session)
        socket = factory.last
        assert first.closed is True
        assert socket is not first

    assert session.state is SessionState.CONNECTED
    assert socket.subscribed[-2:] == ["NSE:SBIN-EQ", "NSE:TCS-EQ"]


def test_replay_releases_a_symbol_dropped_while_it_ran() -> None:
    snapshots = [["NSE:SBIN-EQ", "NSE:TCS-EQ"], ["NSE:SBIN-EQ"]]
    session, factory = make_session(desired=lambda: snapshots.pop(0) if snapshots else [])

    started(session)

    assert factory.last.subscribed == ["NSE:SBIN-EQ", "NSE:TCS-EQ"]
    assert factory.last.unsubscribed == ["NSE:TCS-EQ"]


@pytest.mark.parametrize("final", [SessionState.CLOSED, SessionState.IDLE])
def test_after_close_or_stop_nothing_reconnects_or_replays(final: SessionState) -> None:
    session, factory = make_session(desired=lambda: ["NSE:SBIN-EQ"])
    started(session)
    socket = factory.last

    if final is SessionState.CLOSED:
        session.close()
    else:
        session.stop()
    socket.sdk_reconnect()  # a late SDK auto-reconnect of the closed socket
    socket.emit({"symbol": "NSE:SBIN-EQ", "ltp": 1.0})

    assert session.state is final
    assert socket.closed is True
    assert socket.subscribed == ["NSE:SBIN-EQ"]  # the first replay only
    assert session.is_connected() is False
    assert session.subscribe(["NSE:TCS-EQ"]) is False
    assert len(factory.sockets) == 1


def test_close_stops_a_runner_that_is_backing_off() -> None:
    factory = FakeSocketFactory(comes_up=False)
    runner: list[Any] = []
    session, _ = make_session(factory, spawn=runner.append, desired=lambda: ["NSE:SBIN-EQ"])
    session.retry = RetryPolicy(
        max_attempts=10, connect_timeout_s=0.02, poll_s=0.005, backoff_base_s=30.0
    )
    started(session)
    thread = threading.Thread(target=runner[0], daemon=True)
    thread.start()
    wait_until_backing_off(factory)
    # A late SDK auto-reconnect of the failed socket does not count: it was let go of.
    factory.last.sdk_reconnect()
    assert session.state is SessionState.CONNECTING
    assert factory.last.subscribed == []

    session.close()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(factory.sockets) == 1
    assert session.state is SessionState.CLOSED


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("close", SessionState.DROPPED),
        ("error_socket_down", SessionState.DROPPED),
        ("error_socket_up", SessionState.CONNECTED),  # a non-fatal SDK error (bad symbol)
    ],
)
def test_an_error_or_close_moves_the_state_on(event: str, expected: SessionState) -> None:
    session, factory = make_session()
    started(session)

    if event == "close":
        factory.last.drop()
    else:
        factory.last.error(still_connected=event == "error_socket_up")

    assert session.state is expected


@pytest.mark.parametrize(
    ("factory_fails", "token", "attempts", "sockets"),
    [
        (False, "CLIENT:tok", 3, 3),  # the socket never comes up
        (True, "CLIENT:tok", 3, 0),  # the SDK is missing
        (False, None, 3, 0),  # logged out
    ],
)
def test_the_runner_gives_up_after_its_attempts(
    factory_fails: bool, token: str | None, attempts: int, sockets: int
) -> None:
    factory = FakeSocketFactory(comes_up=False)
    if factory_fails:
        factory.fail = RuntimeError("fyers-apiv3 not installed")
    calls: list[int] = []

    def token_source() -> str | None:
        calls.append(1)
        return token

    session, _ = make_session(factory, token_source=token_source)
    started(session)

    assert session.state is SessionState.FAILED
    assert len(calls) == attempts
    assert len(factory.sockets) == sockets
    assert all(s.closed for s in factory.sockets)
    assert session.busy is False


def test_connecting_again_fetches_a_fresh_token() -> None:
    tokens = iter(["CLIENT:tok-1", "CLIENT:tok-2", "CLIENT:tok-3"])
    session, factory = make_session(token_source=lambda: next(tokens))

    started(session)
    factory.last.drop()
    started(session)  # monitor reconnect after a drop
    session.close()
    started(session)  # explicit connect after a re-login

    assert [s.token for s in factory.sockets] == ["CLIENT:tok-1", "CLIENT:tok-2", "CLIENT:tok-3"]
    assert [s.closed for s in factory.sockets] == [True, True, False]


def test_start_is_a_no_op_while_connected() -> None:
    session, factory = make_session()
    started(session)

    assert session.start() is False
    assert session.busy is True
    assert len(factory.sockets) == 1


def test_a_silently_dropped_socket_reads_as_not_connected_and_can_restart() -> None:
    session, factory = make_session()
    started(session)
    factory.last.connected = False  # no callback at all

    assert session.is_connected() is False
    assert session.busy is False
    started(session)
    assert len(factory.sockets) == 2


def test_a_raising_callback_does_not_kill_the_session(caplog: pytest.LogCaptureFixture) -> None:
    received: list[dict[str, Any]] = []

    def on_message(message: dict[str, Any]) -> None:
        received.append(message)
        if message.get("bad"):
            raise ValueError("bad tick")

    def desired() -> list[str]:
        raise RuntimeError("registry unavailable")

    session, factory = make_session(
        desired=desired, on_message=on_message, token_source=lambda: "CLIENT:s3cr3t-token"
    )
    with caplog.at_level(logging.DEBUG):
        started(session)  # the replay raises inside the SDK's on_connect
        socket = factory.last
        socket.emit({"bad": True})
        socket.emit({"symbol": "NSE:SBIN-EQ"})
        socket.sdk_reconnect()  # the replay raises again

    assert session.state is SessionState.CONNECTED
    assert received == [{"bad": True}, {"symbol": "NSE:SBIN-EQ"}]
    assert session.subscribe(["NSE:TCS-EQ"]) is True
    assert socket.subscribed == ["NSE:TCS-EQ"]
    assert "s3cr3t" not in caplog.text  # the token never reaches the log


def test_a_raising_close_does_not_stop_the_teardown() -> None:
    session, factory = make_session()
    started(session)

    def broken_close() -> None:
        raise RuntimeError("already closed")

    factory.last.close_connection = broken_close  # type: ignore[method-assign]
    session.close()

    assert session.state is SessionState.CLOSED
    assert session.has_socket is False
