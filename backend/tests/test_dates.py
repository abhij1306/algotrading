"""parse_iso_date accepts only YYYY-MM-DD, and every endpoint that takes a date answers an
invalid one with the same fixed 400."""

from collections.abc import Generator
from datetime import date
from types import SimpleNamespace
from typing import NoReturn

import pytest
from fastapi.testclient import TestClient

from app.exceptions import InvalidDateError
from app.main import app
from app.routers import strategies, universe
from app.services import backtest_phase1_service
from app.utils.dates import parse_iso_date, parse_optional_iso_date

BAD = "2025-13-40"


def invalid(field: str) -> dict[str, object]:
    return {
        "error": {
            "code": "INVALID_DATE",
            "message": f"Invalid {field}: expected YYYY-MM-DD",
            "details": {"field": field},
        }
    }


def _must_not_run(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("an invalid date must be rejected before the service runs")


@pytest.fixture
def api() -> Generator[TestClient, None, None]:
    yield TestClient(app)


# --- parse_iso_date ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2025-01-31", date(2025, 1, 31)),
        ("2024-02-29", date(2024, 2, 29)),
        ("2025-1-2", date(2025, 1, 2)),
    ],
)
def test_parse_iso_date_returns_the_calendar_date(value: str, expected: date) -> None:
    assert parse_iso_date(value, field="start_date") == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        " 2025-01-31",
        "2025-01-31 ",
        "2025-01-31T09:15:00",
        "2025-01-31 09:15:00",
        "31-01-2025",
        "2025/01/31",
        "2025-02-30",
        "2025-13-01",
        "not-a-date",
    ],
)
def test_parse_iso_date_rejects_anything_but_a_bare_iso_date(value: str) -> None:
    with pytest.raises(InvalidDateError) as excinfo:
        parse_iso_date(value, field="start_date")

    assert excinfo.value.status_code == 400
    assert excinfo.value.to_dict() == invalid("start_date")


def test_parse_optional_iso_date_treats_missing_and_empty_as_none() -> None:
    assert parse_optional_iso_date(None, field="scan_date") is None
    assert parse_optional_iso_date("", field="scan_date") is None
    assert parse_optional_iso_date("2025-03-04", field="scan_date") == date(2025, 3, 4)


def test_parse_optional_iso_date_still_rejects_a_bad_value() -> None:
    with pytest.raises(InvalidDateError) as excinfo:
        parse_optional_iso_date(BAD, field="scan_date")

    assert excinfo.value.to_dict() == invalid("scan_date")


# --- one invalid-date request per call site ------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        f"/api/data/snapshot/stock?date={BAD}&symbol=SBIN",
        f"/api/data/snapshot/universe?date={BAD}",
    ],
)
def test_data_snapshot_rejects_an_invalid_date(api: TestClient, path: str) -> None:
    response = api.get(path)

    assert response.status_code == 400
    assert response.json() == invalid("date")


def test_vcp_scan_run_rejects_an_invalid_scan_date(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(strategies.vcp_service, "run_scan", _must_not_run)

    response = api.post("/api/strategies/vcp/scan/run", json={"scan_date": BAD})

    assert response.status_code == 400
    assert response.json() == invalid("scan_date")


def test_vcp_signal_rejects_an_invalid_scan_date_with_400(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Was 404 before parse_iso_date: a malformed date is a bad request, not a missing signal.
    monkeypatch.setattr(strategies.vcp_service, "get_signal_detail", _must_not_run)

    response = api.get(f"/api/strategies/vcp/signal/SBIN?scan_date={BAD}")

    assert response.status_code == 400
    assert response.json() == invalid("scan_date")


def test_vcp_signal_passes_a_valid_scan_date_through(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_detail(symbol: str, scan_date: date | None) -> dict[str, object]:
        seen.update(symbol=symbol, scan_date=scan_date)
        return {"symbol": symbol}

    monkeypatch.setattr(strategies.vcp_service, "get_signal_detail", fake_detail)

    response = api.get("/api/strategies/vcp/signal/SBIN?scan_date=2025-03-04")

    assert response.status_code == 200
    assert seen == {"symbol": "SBIN", "scan_date": date(2025, 3, 4)}


@pytest.mark.parametrize("field", ["start_date", "end_date"])
def test_backtest_run_rejects_an_invalid_date(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.setattr(
        backtest_phase1_service, "get_db_session", lambda: SimpleNamespace(close=lambda: None)
    )
    body = {"start_date": "2025-01-01", "end_date": "2025-01-31", field: BAD}

    response = api.post("/api/backtest/run", json=body)

    assert response.status_code == 400
    assert response.json() == invalid(field)


@pytest.mark.parametrize(
    "path",
    [
        f"/api/universe/constituents/NIFTY50?target_date={BAD}",
        f"/api/universe/symbols/NIFTY50?target_date={BAD}",
    ],
)
def test_universe_lookup_rejects_an_invalid_target_date(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr(universe, "get_universe_service", SimpleNamespace)

    response = api.get(path)

    assert response.status_code == 400
    assert response.json() == invalid("target_date")


@pytest.mark.parametrize(
    ("query", "field"),
    [
        (f"start_date={BAD}&end_date=2025-01-31", "start_date"),
        (f"start_date=2025-01-01&end_date={BAD}", "end_date"),
    ],
)
def test_universe_changes_rejects_an_invalid_date(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, query: str, field: str
) -> None:
    monkeypatch.setattr(universe, "get_universe_service", SimpleNamespace)

    response = api.get(f"/api/universe/changes/NIFTY50?{query}")

    assert response.status_code == 400
    assert response.json() == invalid(field)
