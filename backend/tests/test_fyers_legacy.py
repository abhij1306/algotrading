"""What is left of the legacy Fyers stack after K2 moved it into HAT: the DEV_MODE switch, the
legacy client singleton and the hooks that hang the legacy live feed on HAT's Fyers login. The
login, the token file and the session are HAT's and are verified under `tests/hat`."""

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import pytest

from app import config as config_module
from app.hat.execution.fyers_auth import FyersAuthService, FyersCredentials
from app.hat.execution.fyers_session import FyersSession
from app.hat.execution.fyers_tokens import StoredToken, TokenStore, TokenStoreError
from app.services import fyers_client as fyers_client_module
from app.services import live_market_service as lms
from app.services.fyers import auth_flow
from app.services.fyers_client import FyersClient
from app.services.live.provider_session import SessionState

from fakes.data_socket import connected_live, make_live

REPLY = {"s": "ok", "access_token": "tok-secret-abc", "fy_id": "XA1234", "refresh_token": "r"}
CREDS = FyersCredentials("CLIENT-100", "app-secret", "https://example.test/callback")


class FakeSession:
    def __init__(self, **_kwargs: str) -> None:
        pass

    def set_token(self, _auth_code: str) -> None:
        pass

    def generate_token(self) -> dict[str, Any]:
        return REPLY


@pytest.fixture
def store(tmp_path: Path) -> TokenStore:
    return TokenStore(tmp_path / "config" / "access_token.json")


def make_service(store: TokenStore) -> FyersAuthService:
    return FyersAuthService(
        store, session_factory=FakeSession, credentials=lambda: CREDS, on_token_change=lambda: None
    )


# --- config.fyers_available and DEV_MODE ----------------------------------------------------


@pytest.mark.parametrize("flag", ["true", "1", " Yes ", "ON"])
def test_fyers_is_unavailable_in_dev_mode_even_with_a_usable_token(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    # The legacy FyersClient skips loading the token under DEV_MODE, so quotes must not pick Fyers.
    monkeypatch.setattr(config_module, "token_store", store)
    store.save(REPLY, client_id="CLIENT-100")
    monkeypatch.setenv("DEV_MODE", flag)

    assert config_module.fyers_available() is False

    monkeypatch.setenv("DEV_MODE", "false")
    assert config_module.fyers_available() is True


def test_fyers_available_reads_the_store_at_call_time(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_module, "token_store", store)
    monkeypatch.delenv("DEV_MODE", raising=False)
    monkeypatch.delenv("FYERS_CLIENT_ID", raising=False)
    monkeypatch.delenv("FYERS_ACCESS_TOKEN", raising=False)
    assert config_module.fyers_available() is False

    store.save(REPLY, client_id="CLIENT-100")  # login after import
    assert config_module.fyers_available() is True

    store.save(REPLY, client_id="CLIENT-100", now=datetime(2020, 1, 1, tzinfo=UTC))
    assert config_module.fyers_available() is False  # expired

    store.path.write_bytes(b"\xff\xfe")
    assert config_module.fyers_available() is False  # undecodable


def test_fyers_available_ignores_env_credentials_the_client_cannot_use(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_module, "token_store", store)
    monkeypatch.setenv("FYERS_CLIENT_ID", "env-client")
    monkeypatch.setenv("FYERS_ACCESS_TOKEN", "env-token")

    assert config_module.fyers_available() is False


def test_fyers_available_is_false_when_the_token_file_is_unreadable(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied() -> NoReturn:
        raise TokenStoreError("Fyers token file could not be read")

    monkeypatch.setattr(store, "load", denied)
    monkeypatch.setattr(config_module, "token_store", store)

    assert config_module.fyers_available() is False


# --- the legacy client singleton ------------------------------------------------------------


def test_the_legacy_client_loads_nothing_under_dev_mode(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.hat.execution.fyers_session as session_module

    store.save(REPLY, client_id="CLIENT-100")
    monkeypatch.setattr(session_module, "token_store", store)
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setattr(FyersClient, "_instance", None)

    client = FyersClient()

    assert (client.fyers, client.access_token) == (None, None)
    assert FyersClient() is client  # still the process singleton


def test_the_legacy_client_loads_the_token_through_the_hat_session(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.hat.execution.fyers_session as session_module

    store.save(REPLY, client_id="CLIENT-100")
    monkeypatch.setattr(session_module, "token_store", store)
    monkeypatch.delenv("DEV_MODE", raising=False)
    monkeypatch.setattr(FyersClient, "_instance", None)

    client = FyersClient()

    assert client.fyers is not None  # a FyersModel built offline from the stored token
    assert (client.client_id, client.access_token) == ("CLIENT-100", "tok-secret-abc")
    assert isinstance(store.load(), StoredToken)


def test_reset_fyers_client_drops_the_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FyersClient, "_instance", object())
    monkeypatch.setattr(fyers_client_module, "_fyers_client", object())

    fyers_client_module.reset_fyers_client()

    assert FyersClient._instance is None
    assert fyers_client_module._fyers_client is None


# --- the legacy hooks on HAT's login --------------------------------------------------------


def test_logout_is_an_explicit_live_feed_disconnect(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_market, factory = connected_live()
    live_market.dev_mode = True
    monkeypatch.setattr(lms, "live_market", live_market)
    service = make_service(store)
    auth_flow.install_legacy_hooks(service)

    service.disconnect()

    assert factory.last.closed is True
    assert live_market.session.state is SessionState.CLOSED
    # The feed stays down until an explicit connect, even after a new login.
    assert live_market._explicitly_disconnected is True
    assert live_market.connect(prevalidated_token=True) == "disconnected"


def test_installing_the_legacy_hooks_twice_registers_them_once(store: TokenStore) -> None:
    service = make_service(store)

    auth_flow.install_legacy_hooks(service)
    auth_flow.install_legacy_hooks(service)

    assert service._hooks["token_change"][1:] == [auth_flow._reset_fyers_client]
    assert service._hooks["login"] == [auth_flow._connect_live_feed]
    assert service._hooks["disconnect"] == [auth_flow._disconnect_live_feed]


def test_a_failed_live_feed_reconnect_does_not_fail_the_login(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service(store)
    service.add_hook("login", lambda: (_ for _ in ()).throw(RuntimeError("leak-marker socket")))

    assert service.exchange("code").connected is True
    assert store.load() is not None


@pytest.mark.parametrize(
    ("socket_open", "expected_disconnects"),
    [(True, [1]), (False, [])],  # only a socket built with the old token is closed first
)
def test_default_login_hook_runs_an_explicit_connect_on_the_app_loop(
    monkeypatch: pytest.MonkeyPatch, socket_open: bool, expected_disconnects: list[int]
) -> None:
    live_market, _factory = connected_live() if socket_open else make_live()
    assert live_market.has_provider_socket() is socket_open
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

    assert disconnects == expected_disconnects
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


def test_dev_mode_client_never_loads_a_token_even_when_validated(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(REPLY, client_id="CLIENT-100")
    monkeypatch.setattr(fyers_client_module, "fyers_client_disabled", lambda: True)
    client = object.__new__(FyersClient)
    FyersSession.__init__(client, store, load=False, model_factory=lambda **_k: object())

    assert client.validate_token() is False
    assert client.fyers is None
