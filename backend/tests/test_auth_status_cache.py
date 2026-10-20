"""Fyers status after a token exchange: the 30s status cache must not hide a fresh login."""

from pathlib import Path
from typing import Any

import pytest

from app.routers import auth
from app.services import fyers_client as fyers_client_module


class FakeSession:
    def __init__(self, **_kwargs: str) -> None:
        self.auth_code = ""

    def set_token(self, auth_code: str) -> None:
        self.auth_code = auth_code

    def generate_token(self) -> dict[str, Any]:
        return {"s": "ok", "access_token": "token-123", "fy_id": "XA1234"}


@pytest.fixture
def token_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(auth, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(auth, "ACCESS_TOKEN_FILE", str(tmp_path / "access_token.json"))
    monkeypatch.setattr(auth.fyersModel, "SessionModel", FakeSession)
    monkeypatch.setattr(fyers_client_module, "reset_fyers_client", lambda: None)
    monkeypatch.setenv("CLIENT_ID", "client")
    monkeypatch.setenv("SECRET_KEY", "secret")
    monkeypatch.setenv("REDIRECT_URI", "https://example.test/")
    monkeypatch.setitem(auth._status_cache, "cached_at", 0)
    return tmp_path


def test_status_reads_connected_right_after_token_exchange(token_dir: Path) -> None:
    # A status check before login caches "disconnected" for 30s.
    assert auth.get_fyers_status() == {"connected": False, "user_id": None}

    auth.generate_token(auth.AuthCodeRequest(auth_code="abc123"))

    assert (token_dir / "access_token.json").exists()
    assert auth.get_fyers_status() == {"connected": True, "user_id": "XA1234"}
