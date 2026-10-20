"""Fyers status after a token exchange: the 30s status cache must not hide a fresh login."""

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.fyers.auth_flow import FyersAuthService, FyersCredentials, get_fyers_auth
from app.services.fyers.token_store import TokenStore


class FakeSession:
    def __init__(self, **_kwargs: str) -> None:
        self.auth_code = ""

    def generate_authcode(self) -> str:
        return "https://login.example.test/"

    def set_token(self, auth_code: str) -> None:
        self.auth_code = auth_code

    def generate_token(self) -> dict[str, Any]:
        return {"s": "ok", "access_token": "token-123", "fy_id": "XA1234"}


@pytest.fixture
def token_dir(tmp_path: Path) -> Generator[Path, None, None]:
    service = FyersAuthService(
        TokenStore(tmp_path / "access_token.json"),
        session_factory=FakeSession,
        credentials=lambda: FyersCredentials("client", "secret", "https://example.test/"),
        on_token_change=lambda: None,
        on_disconnect=lambda: None,
    )
    app.dependency_overrides[get_fyers_auth] = lambda: service
    yield tmp_path
    app.dependency_overrides.pop(get_fyers_auth, None)


def test_status_reads_connected_right_after_token_exchange(token_dir: Path) -> None:
    api = TestClient(app)
    # A status check before login caches "disconnected" for 30s.
    assert api.get("/api/auth/fyers/status").json() == {"connected": False, "user_id": None}

    api.post("/api/auth/fyers/token", json={"auth_code": "abc123"})

    assert (token_dir / "access_token.json").exists()
    assert api.get("/api/auth/fyers/status").json() == {"connected": True, "user_id": "XA1234"}
