"""Characterization tests for FyersClient.validate_token, pinned ahead of a refactor."""

from typing import Any

import pytest

from app.services.fyers_client import FyersClient


class FakeFyersModel:
    def __init__(self, replies: list[dict[str, Any] | Exception]) -> None:
        self.replies = list(replies)
        self.profile_calls = 0

    def get_profile(self) -> dict[str, Any]:
        self.profile_calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class IsolatedClient(FyersClient):
    """A FyersClient that is not the process singleton and never reads disk."""

    _instance = None

    def __init__(self) -> None:  # skip FyersClient.__init__ (no token file, no SDK)
        self.fyers = None
        self.token_expires_at = None
        self._validating_token = False
        self.events: list[str] = []


def make_client(
    *,
    model: FakeFyersModel | None,
    refresh: list[bool],
    connect_sets: FakeFyersModel | None = None,
    connect_error: Exception | None = None,
    expired: bool = False,
) -> IsolatedClient:
    IsolatedClient._instance = None
    client = IsolatedClient()
    client.fyers = model
    refreshes = list(refresh)

    def fake_refresh() -> bool:
        client.events.append("refresh")
        return refreshes.pop(0)

    def fake_connect(skip_validation: bool = False, **kwargs: bool) -> None:
        client.events.append("connect" + ("-quiet" if kwargs else ""))
        if connect_error is not None:
            raise connect_error
        if connect_sets is not None:
            client.fyers = connect_sets

    client._refresh_credentials_if_changed = fake_refresh  # type: ignore[method-assign]
    client._connect = fake_connect  # type: ignore[method-assign]
    client.is_token_expired = lambda: expired  # type: ignore[method-assign]
    return client


OK = {"s": "ok"}
BAD = {"s": "error", "message": "invalid token"}


def test_nested_validation_reports_whether_a_client_exists() -> None:
    client = make_client(model=FakeFyersModel([]), refresh=[])
    client._validating_token = True

    assert client.validate_token() is True
    assert client.events == []


def test_no_client_after_reconnect_is_invalid() -> None:
    client = make_client(model=None, refresh=[False])

    assert client.validate_token() is False
    assert client.events == ["refresh", "connect-quiet"]
    assert client._validating_token is False


def test_missing_client_is_connected_once_then_validated() -> None:
    model = FakeFyersModel([OK])
    client = make_client(model=None, refresh=[False], connect_sets=model)

    assert client.validate_token() is True
    assert client.events == ["refresh", "connect-quiet"]


def test_expired_token_skips_the_profile_call() -> None:
    model = FakeFyersModel([OK])
    client = make_client(model=model, refresh=[False], expired=True)

    assert client.validate_token() is False
    assert model.profile_calls == 0


def test_token_changed_on_disk_reconnects_before_validating() -> None:
    model = FakeFyersModel([OK])
    client = make_client(model=model, refresh=[True])

    assert client.validate_token() is True
    assert client.events == ["refresh", "connect"]


@pytest.mark.parametrize(
    ("replies", "refresh", "expected", "events", "profile_calls"),
    [
        ([OK], [False], True, ["refresh"], 1),
        ([BAD], [False, False], False, ["refresh", "refresh"], 1),
        ([BAD, OK], [False, True], True, ["refresh", "refresh", "connect"], 2),
        ([BAD, BAD], [False, True], False, ["refresh", "refresh", "connect"], 2),
        ([RuntimeError("net")], [False, False], False, ["refresh", "refresh"], 1),
        ([RuntimeError("net"), OK], [False, True], True, ["refresh", "refresh", "connect"], 2),
        (
            [RuntimeError("net"), RuntimeError("again")],
            [False, True],
            False,
            ["refresh", "refresh", "connect"],
            2,
        ),
    ],
)
def test_profile_check_and_single_retry_after_relogin(
    replies: list[dict[str, Any] | Exception],
    refresh: list[bool],
    expected: bool,
    events: list[str],
    profile_calls: int,
) -> None:
    model = FakeFyersModel(replies)
    client = make_client(model=model, refresh=refresh)

    assert client.validate_token() is expected
    assert client.events == events
    assert model.profile_calls == profile_calls
    assert client._validating_token is False


def test_retry_connect_failure_after_profile_error_is_invalid() -> None:
    model = FakeFyersModel([RuntimeError("net")])
    client = make_client(model=model, refresh=[False, True], connect_error=RuntimeError("sdk"))

    assert client.validate_token() is False
    assert client.events == ["refresh", "refresh", "connect"]
