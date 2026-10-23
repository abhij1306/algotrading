"""CORS allows the configured frontend origins and loopback pages only (utils/origins.py)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.utils import origins
from app.utils.origins import FrontendCORSMiddleware


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(origins, "ALLOWED_ORIGINS", ("http://desk.lan:3000", "null"))
    app = FastAPI()
    app.add_middleware(
        FrontendCORSMiddleware,
        allow_origins=list(origins.ALLOWED_ORIGINS),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"ok": "yes"}

    return TestClient(app)


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        pytest.param("http://desk.lan:3000", True, id="configured"),
        pytest.param("http://localhost:3002", True, id="loopback-fallback-port"),
        pytest.param("http://127.0.0.1:5173", True, id="loopback-other-port"),
        pytest.param("http://[::1]:3000", True, id="ipv6-loopback"),
        pytest.param("https://evil.com", False, id="other-site"),
        pytest.param("http://localhost.evil.com", False, id="localhost-prefix"),
        pytest.param("http://localhost@evil.com", False, id="userinfo"),
        pytest.param("null", False, id="null-even-if-configured"),
    ],
)
def test_cors_allows_frontend_origins_only(client: TestClient, origin: str, allowed: bool) -> None:
    simple = client.get("/ping", headers={"origin": origin})
    preflight = client.options(
        "/ping",
        headers={"origin": origin, "access-control-request-method": "GET"},
    )

    expected = origin if allowed else None
    assert simple.headers.get("access-control-allow-origin") == expected
    assert preflight.headers.get("access-control-allow-origin") == expected
    assert preflight.status_code == (200 if allowed else 400)
