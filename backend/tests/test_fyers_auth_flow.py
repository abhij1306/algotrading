"""FyersAuthService and the /api/auth/fyers routes, with a fake SessionModel (no Fyers calls)."""

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Generator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import live_market_service as lms
from app.services.fyers import auth_flow
from app.services.fyers.auth_flow import (
    FyersAuthService,
    FyersCredentials,
    FyersStatus,
    MissingCredentialsError,
    TokenRejectedError,
    credentials_from_env,
    get_fyers_auth,
)
from app.services.fyers.token_store import TokenStore
from app.services.live.provider_session import SessionState

from fakes.data_socket import connected_live

SECRET_TOKEN = "tok-very-secret-123"
OK_REPLY = {"s": "ok", "access_token": SECRET_TOKEN, "fy_id": "XA1234"}
Installer = Callable[[FyersAuthService], TestClient]
CREDS = FyersCredentials("CLIENT-100", "app-secret", "https://example.test/callback")


class FakeSession:
    """Stands in for fyersModel.SessionModel; records what the flow asked of it."""

    def __init__(self, reply: Mapping[str, Any] | Exception) -> None:
        self.reply = reply
        self.kwargs: dict[str, str] = {}
        self.auth_code: str | None = None

    def __call__(self, **kwargs: str) -> "FakeSession":
        self.kwargs = kwargs
        return self

    def generate_authcode(self) -> str:
        return f"https://login.example.test/?client_id={self.kwargs['client_id']}"

    def set_token(self, auth_code: str) -> None:
        self.auth_code = auth_code

    def generate_token(self) -> Mapping[str, Any]:
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class Recorder:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    def __call__(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_service(
    tmp_path: Path,
    reply: Mapping[str, Any] | Exception = OK_REPLY,
    *,
    on_token_change: Recorder | None = None,
    on_disconnect: Recorder | None = None,
    on_login: Recorder | None = None,
    credentials: Callable[[], FyersCredentials] = lambda: CREDS,
    clock: Clock | None = None,
) -> tuple[FyersAuthService, FakeSession]:
    session = FakeSession(reply)
    service = FyersAuthService(
        TokenStore(tmp_path / "access_token.json"),
        session_factory=session,
        credentials=credentials,
        on_token_change=on_token_change or Recorder(),
        on_disconnect=on_disconnect or Recorder(),
        on_login=on_login or Recorder(),
        clock=clock or Clock(),
    )
    return service, session


# --- service --------------------------------------------------------------------------------


def test_login_url_comes_from_the_session(tmp_path: Path) -> None:
    service, session = make_service(tmp_path)

    assert service.login_url() == "https://login.example.test/?client_id=CLIENT-100"
    assert session.kwargs["redirect_uri"] == "https://example.test/callback"
    assert session.kwargs["response_type"] == "code"


def test_exchange_saves_the_token_and_refreshes_the_client(tmp_path: Path) -> None:
    refresh = Recorder()
    service, session = make_service(tmp_path, on_token_change=refresh)

    assert service.exchange("auth-code-1") == FyersStatus(connected=True, user_id="XA1234")

    assert session.auth_code == "auth-code-1"
    saved = service.store.load()
    assert saved is not None
    assert (saved.access_token, saved.client_id, saved.user_id) == (
        SECRET_TOKEN,
        "CLIENT-100",
        "XA1234",
    )
    assert refresh.calls == 1


def test_exchange_drops_a_cached_disconnected_status(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    assert service.status() == FyersStatus(connected=False, user_id=None)

    service.exchange("auth-code-1")

    assert service.status() == FyersStatus(connected=True, user_id="XA1234")


def test_status_is_cached_for_30_seconds(tmp_path: Path) -> None:
    clock = Clock()
    service, _ = make_service(tmp_path, clock=clock)
    assert service.status().connected is False

    service.store.save(OK_REPLY, client_id="CLIENT-100")  # written behind the service's back
    clock.now += 29
    assert service.status().connected is False

    clock.now += 2
    assert service.status() == FyersStatus(connected=True, user_id="XA1234")


def test_an_expired_token_reads_as_disconnected(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    service.store.save(OK_REPLY, client_id="CLIENT-100", now=datetime(2020, 1, 1, tzinfo=UTC))

    assert service.status() == FyersStatus(connected=False, user_id=None)


@pytest.mark.parametrize(
    "document",
    [
        {"access_token": "t", "client_id": "c", "fy_id": "XA1234"},  # no expires_at
        {"access_token": "t", "fy_id": "XA1234", "expires_at": "2999-01-01T00:00:00+00:00"},
    ],
    ids=["no-expiry", "no-client-id"],
)
def test_an_unusable_token_reads_as_disconnected(tmp_path: Path, document: dict[str, str]) -> None:
    service, _ = make_service(tmp_path)
    service.store.path.write_text(json.dumps(document), encoding="utf-8")

    assert service.status() == FyersStatus(connected=False, user_id=None)


def test_rejected_code_saves_nothing_and_keeps_the_client(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    refresh = Recorder()
    service, _ = make_service(
        tmp_path,
        {"s": "error", "code": -413, "message": "leak-marker echoed abc"},
        on_token_change=refresh,
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(TokenRejectedError) as excinfo:
        service.exchange("bad-code")

    assert excinfo.value.message == "Fyers rejected the auth code. Please log in again."
    assert "leak-marker" not in caplog.text
    assert service.store.load() is None
    assert refresh.calls == 0


def test_reply_without_a_token_is_rejected_with_a_fixed_message(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, {"s": "ok"})

    with pytest.raises(TokenRejectedError) as excinfo:
        service.exchange("code")

    assert excinfo.value.message == "Fyers rejected the auth code. Please log in again."


def test_a_failed_client_refresh_does_not_fail_the_login(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, on_token_change=Recorder(RuntimeError("boom")))

    assert service.exchange("code") == FyersStatus(connected=True, user_id="XA1234")
    assert service.store.load() is not None


def test_exchange_never_logs_the_token(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    service, _ = make_service(tmp_path)

    with caplog.at_level(logging.DEBUG):
        service.exchange("code")

    assert caplog.records
    assert SECRET_TOKEN not in caplog.text


def test_disconnect_stops_the_feed_removes_the_token_and_drops_the_client(
    tmp_path: Path,
) -> None:
    feed = Recorder()
    refresh = Recorder()
    service, _ = make_service(tmp_path, on_disconnect=feed, on_token_change=refresh)
    service.exchange("code")
    assert service.status().connected is True
    assert refresh.calls == 1

    service.disconnect()

    assert feed.calls == 1
    assert refresh.calls == 2  # the in-memory client is reset, so the old token is gone
    assert service.store.load() is None
    assert service.status() == FyersStatus(connected=False, user_id=None)


def test_logout_is_an_explicit_live_feed_disconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_market, factory = connected_live()
    live_market.dev_mode = True
    monkeypatch.setattr(lms, "live_market", live_market)
    service = FyersAuthService(
        TokenStore(tmp_path / "access_token.json"), on_token_change=Recorder()
    )

    service.disconnect()

    assert factory.last.closed is True
    assert live_market.session.state is SessionState.CLOSED
    # The feed stays down until an explicit connect, even after a new login.
    assert live_market._explicitly_disconnected is True
    assert live_market.connect(prevalidated_token=True) == "disconnected"


def test_login_after_logout_reconnects_the_live_feed_once(tmp_path: Path) -> None:
    feed_down = Recorder()
    feed_up = Recorder()
    service, _ = make_service(tmp_path, on_disconnect=feed_down, on_login=feed_up)
    service.exchange("code-1")
    service.disconnect()
    feed_up.calls = 0

    service.exchange("code-2")

    assert (feed_down.calls, feed_up.calls) == (1, 1)


def test_rejected_login_does_not_reconnect_the_live_feed(tmp_path: Path) -> None:
    feed_up = Recorder()
    service, _ = make_service(tmp_path, {"s": "error", "code": -413}, on_login=feed_up)

    with pytest.raises(TokenRejectedError):
        service.exchange("bad-code")

    assert feed_up.calls == 0


def test_a_failed_live_feed_reconnect_does_not_fail_the_login(
    tmp_path: Path, use_service: Installer
) -> None:
    service, _ = make_service(tmp_path, on_login=Recorder(RuntimeError("leak-marker socket")))
    api = use_service(service)

    response = api.post("/api/auth/fyers/token", json={"auth_code": "code"})

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert service.store.load() is not None


def test_default_login_hook_runs_an_explicit_connect_on_the_app_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_market, _factory = connected_live()  # a socket built with the old token
    monkeypatch.setattr(lms, "live_market", live_market)

    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever, daemon=True)
    worker.start()
    calls: list[tuple[asyncio.AbstractEventLoop, bool, asyncio.AbstractEventLoop]] = []
    done = threading.Event()

    async def fake_connect_async(
        loop: asyncio.AbstractEventLoop | None = None, explicit: bool = False, **_: object
    ) -> str:
        calls.append((loop, explicit, asyncio.get_running_loop()))
        done.set()
        return "market_closed"

    disconnects: list[int] = []

    def fake_disconnect() -> None:
        assert not calls, "the old socket must close before the new connect"
        disconnects.append(1)

    monkeypatch.setattr(live_market, "loop", loop)
    monkeypatch.setattr(live_market, "disconnect", fake_disconnect)
    monkeypatch.setattr(live_market, "connect_async", fake_connect_async)
    try:
        auth_flow._connect_live_feed()
        assert done.wait(timeout=5)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        worker.join(timeout=5)
        loop.close()

    assert disconnects == [1]
    assert calls == [(loop, True, loop)]


def test_default_login_hook_without_an_app_loop_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.live_market_service import live_market

    def fail(**_: object) -> NoReturn:
        raise AssertionError("connect must not be scheduled without a loop")

    monkeypatch.setattr(live_market, "loop", None)
    monkeypatch.setattr(live_market, "connect_async", fail)

    auth_flow._connect_live_feed()  # no loop, no error


def test_credentials_from_env_names_the_missing_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIENT_ID", "c")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("FYERS_SECRET_KEY", raising=False)
    monkeypatch.setenv("REDIRECT_URI", "https://example.test/")

    with pytest.raises(MissingCredentialsError) as excinfo:
        credentials_from_env()

    assert excinfo.value.message == (
        "SECRET_KEY not found in environment variables. Please add it to .env file."
    )


def test_credentials_from_env_accepts_the_documented_fyers_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("CLIENT_ID", "SECRET_KEY", "FYERS_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FYERS_APP_ID", "APP-1")
    monkeypatch.setenv("FYERS_SECRET_KEY", "s3")
    monkeypatch.setenv("REDIRECT_URI", "https://example.test/")

    assert credentials_from_env() == FyersCredentials("APP-1", "s3", "https://example.test/")


def test_credentials_from_env_prefers_the_login_flow_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIENT_ID", "APP-legacy")
    monkeypatch.setenv("FYERS_APP_ID", "APP-1")
    monkeypatch.setenv("SECRET_KEY", "s-legacy")
    monkeypatch.setenv("FYERS_SECRET_KEY", "s3")
    monkeypatch.setenv("REDIRECT_URI", "https://example.test/")

    assert credentials_from_env() == FyersCredentials(
        "APP-legacy", "s-legacy", "https://example.test/"
    )


def test_a_logout_during_login_wins(tmp_path: Path, use_service: Installer) -> None:
    refresh = Recorder()
    feed_up = Recorder()
    service, session = make_service(tmp_path, on_token_change=refresh, on_login=feed_up)

    def generate_token_while_user_logs_out() -> Mapping[str, Any]:
        service.disconnect()  # the logout request lands while Fyers is answering
        return OK_REPLY

    session.generate_token = generate_token_while_user_logs_out  # type: ignore[method-assign]
    api = use_service(service)

    response = api.post("/api/auth/fyers/token", json={"auth_code": "code"})

    assert response.status_code == 400
    assert response.json() == {"detail": "Login was cancelled by a logout. Please log in again."}
    assert service.store.load() is None
    assert service.status() == FyersStatus(connected=False, user_id=None)
    assert feed_up.calls == 0


def test_a_login_after_a_logout_still_saves(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    service.disconnect()

    assert service.exchange("code") == FyersStatus(connected=True, user_id="XA1234")
    assert service.store.load() is not None


def test_process_service_uses_the_shared_token_store() -> None:
    from app.services.fyers.token_store import token_store

    assert auth_flow.fyers_auth.store is token_store


# --- routes ---------------------------------------------------------------------------------


@pytest.fixture
def use_service() -> Generator[Installer, None, None]:
    def install(service: FyersAuthService) -> TestClient:
        app.dependency_overrides[get_fyers_auth] = lambda: service
        return TestClient(app)

    yield install
    app.dependency_overrides.pop(get_fyers_auth, None)


def test_url_route_returns_the_login_url(tmp_path: Path, use_service: Installer) -> None:
    service, _ = make_service(tmp_path)
    api = use_service(service)

    response = api.get("/api/auth/fyers/url")

    assert response.status_code == 200
    assert response.json() == {"url": "https://login.example.test/?client_id=CLIENT-100"}


def test_url_route_reports_the_missing_credential(tmp_path: Path, use_service: Installer) -> None:
    def missing() -> FyersCredentials:
        raise MissingCredentialsError(
            "CLIENT_ID not found in environment variables. Please add it to .env file."
        )

    service, _ = make_service(tmp_path, credentials=missing)
    api = use_service(service)

    response = api.get("/api/auth/fyers/url")

    assert response.status_code == 500
    assert response.json() == {
        "detail": "CLIENT_ID not found in environment variables. Please add it to .env file."
    }


def test_token_route_success(tmp_path: Path, use_service: Installer) -> None:
    service, _ = make_service(tmp_path)
    api = use_service(service)

    response = api.post("/api/auth/fyers/token", json={"auth_code": "code"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "message": "Token generated and saved successfully",
    }
    assert SECRET_TOKEN not in response.text


def test_token_route_maps_a_rejection_to_400(tmp_path: Path, use_service: Installer) -> None:
    service, _ = make_service(
        tmp_path, {"s": "error", "code": -413, "message": "leak-marker echoed abc"}
    )
    api = use_service(service)

    response = api.post("/api/auth/fyers/token", json={"auth_code": "bad"})

    assert response.status_code == 400
    assert response.json() == {"detail": "Fyers rejected the auth code. Please log in again."}


def test_token_route_reports_missing_credentials(tmp_path: Path, use_service: Installer) -> None:
    def missing() -> FyersCredentials:
        raise MissingCredentialsError("SECRET_KEY not found")

    service, _ = make_service(tmp_path, credentials=missing)
    api = use_service(service)

    response = api.post("/api/auth/fyers/token", json={"auth_code": "code"})

    assert response.status_code == 500
    assert response.json() == {
        "detail": "Fyers credentials not found in environment variables. Please check .env file."
    }


def test_token_route_hides_sdk_exception_text(tmp_path: Path, use_service: Installer) -> None:
    service, _ = make_service(tmp_path, RuntimeError("leak-marker app-secret"))
    api = use_service(service)

    response = api.post("/api/auth/fyers/token", json={"auth_code": "code"})

    assert response.status_code == 500
    assert response.json() == {"detail": "Token exchange failed"}


def test_status_and_disconnect_routes(tmp_path: Path, use_service: Installer) -> None:
    service, _ = make_service(tmp_path)
    api = use_service(service)
    api.post("/api/auth/fyers/token", json={"auth_code": "code"})

    assert api.get("/api/auth/fyers/status").json() == {"connected": True, "user_id": "XA1234"}

    response = api.post("/api/auth/fyers/disconnect")

    assert response.json() == {"status": "success", "message": "Disconnected from Fyers"}
    assert api.get("/api/auth/fyers/status").json() == {"connected": False, "user_id": None}
