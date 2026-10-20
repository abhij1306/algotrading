"""The screener routes map service errors to status codes and never echo internal text.

The service is built on fakes (see test_screener_service) and injected by overriding the
`get_screener_service` dependency, so no database or quote feed is involved.
"""

from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from screener_fakes import ROWS, FakeQuotes, FakeRepository, FakeUniverses

from app.main import app
from app.routers.screener import get_screener_service
from app.services.screener import ScreenerService, TTLCache

MARKER = "leak-marker-91c2 password=hunter2"


@pytest.fixture
def universes() -> FakeUniverses:
    return FakeUniverses({"NIFTY50": ["ALPHA", "BRAVO"], "NIFTYBANK": ["ALPHA"]})


@pytest.fixture
def api(universes: FakeUniverses) -> Generator[TestClient, None, None]:
    service = ScreenerService(FakeRepository(list(ROWS)), universes, FakeQuotes(), TTLCache(5.0, 8))
    app.dependency_overrides[get_screener_service] = lambda: service
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_screener_service, None)


def test_results_returns_the_payload(api: TestClient) -> None:
    response = api.get("/api/screener/results", params={"limit": 1, "sort_by": "rsi"})

    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert (body["total"], body["page"], body["limit"]) == (2, 1, 1)
    assert [r["symbol"] for r in body["results"]] == ["BRAVO"]


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({"universe": "Nifty999"}, "Unknown universe: Nifty999"),
        ({"sort_by": "Bogus"}, "Invalid sort field: bogus"),
    ],
)
def test_bad_input_is_a_400_with_a_safe_detail(
    api: TestClient, params: dict[str, str], detail: str
) -> None:
    response = api.get("/api/screener/results", params=params)

    assert (response.status_code, response.json()) == (400, {"detail": detail})


@pytest.mark.parametrize("params", [{"page": 0}, {"limit": 101}, {"universe": ""}])
def test_out_of_range_parameters_are_a_422(api: TestClient, params: dict[str, Any]) -> None:
    assert api.get("/api/screener/results", params=params).status_code == 422


def test_indices_returns_the_universes_with_a_cache_header(api: TestClient) -> None:
    response = api.get("/api/screener/indices")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=3600"
    assert response.json() == {
        "indices": [
            {"id": "NIFTY50", "name": "Nifty 50", "count": 2},
            {"id": "NIFTYBANK", "name": "Nifty BANK", "count": 1},
        ],
        "default": "NIFTY50",
        "total_loaded": 2,
    }


def test_a_failing_universe_list_is_a_500_without_the_cause(
    api: TestClient, universes: FakeUniverses
) -> None:
    universes.error = RuntimeError(MARKER)

    response = api.get("/api/screener/indices")

    assert (response.status_code, response.json()) == (
        500,
        {"detail": "Failed to load screener universes"},
    )
    assert MARKER not in response.text


def test_an_unexpected_results_failure_is_a_generic_500(
    universes: FakeUniverses, api: TestClient
) -> None:
    universes.error = RuntimeError(MARKER)

    response = TestClient(app, raise_server_exceptions=False).get("/api/screener/results")

    assert response.status_code == 500
    assert MARKER not in response.text
