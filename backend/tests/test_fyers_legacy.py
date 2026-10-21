"""What is left of the legacy Fyers stack after K2 moved it into HAT: the legacy client singleton
and the hook that resets it on HAT's Fyers login (K4 removed the DEV_MODE switch). The login, the
token file and the session are HAT's and are verified under `tests/hat`."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import pytest

from app import config as config_module
from app.hat.execution.fyers_auth import FyersAuthService, FyersCredentials
from app.hat.execution.fyers_tokens import StoredToken, TokenStore, TokenStoreError
from app.services import fyers_client as fyers_client_module
from app.services.fyers import auth_flow
from app.services.fyers_client import FyersClient

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


# --- config.fyers_available ------------------------------------------------------------------


def test_fyers_available_reads_the_store_at_call_time(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_module, "token_store", store)
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


def test_the_legacy_client_loads_the_token_through_the_hat_session(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.hat.execution.fyers_session as session_module

    store.save(REPLY, client_id="CLIENT-100")
    monkeypatch.setattr(session_module, "token_store", store)
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


def test_installing_the_legacy_hooks_twice_registers_them_once(store: TokenStore) -> None:
    service = make_service(store)

    auth_flow.install_legacy_hooks(service)
    auth_flow.install_legacy_hooks(service)

    assert service._hooks["token_change"][1:] == [auth_flow._reset_fyers_client]
    # The login and logout hooks are HAT's feed's own (`LiveFeed.install_auth_hooks`, K3).
    assert service._hooks["login"] == []
    assert service._hooks["disconnect"] == []


def test_a_failing_login_hook_does_not_fail_the_login(store: TokenStore) -> None:
    service = make_service(store)
    service.add_hook("login", lambda: (_ for _ in ()).throw(RuntimeError("leak-marker socket")))

    assert service.exchange("code").connected is True
    assert store.load() is not None
