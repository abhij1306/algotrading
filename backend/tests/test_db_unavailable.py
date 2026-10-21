"""A down database answers 503 DATABASE_UNAVAILABLE, leaks no driver text and logs one line instead
of a traceback per request."""

import asyncio
import logging
import sqlite3
from collections.abc import Generator
from typing import NoReturn

import psycopg2
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from app import database
from app import main as app_main
from app.database import get_db
from app.exceptions import DatabaseUnavailableError
from app.main import app
from app.utils.db_errors import is_db_unavailable
from app.utils.logger import DatabaseUnavailableLogFilter

SECRET = "hunter2-secret"
DOWN_BODY = {"error": {"code": "DATABASE_UNAVAILABLE", "message": "Database unavailable"}}
PROBE_PATH = "/__test/db-probe"
DB_DEPENDENCY = Depends(get_db)
DOWN_DB_URL = f"postgresql+psycopg2://postgres:{SECRET}@db.invalid:5432/algotrading"


def _refuse_connection() -> NoReturn:
    raise psycopg2.OperationalError(
        f'connection to server at "db.invalid", port 5432 failed: Connection refused {SECRET}'
    )


def _connection_refused() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception(f"connection refused {SECRET}"))


@pytest.fixture(autouse=True)
def db_probe_route() -> Generator[None, None, None]:
    """A route that queries the database through `get_db`: the app has no DB route of its own
    to probe, and the handlers under test are the app's, not a route's."""

    def probe(db: Session = DB_DEPENDENCY) -> dict[str, int]:
        return {"rows": db.execute(text("SELECT count(*) FROM hat_market")).scalar_one()}

    count = len(app.router.routes)
    app.add_api_route(PROBE_PATH, probe, methods=["GET"], include_in_schema=False)
    yield
    del app.router.routes[count:]


@pytest.fixture
def down_engine() -> Generator[Engine, None, None]:
    """A real Postgres engine whose every connect attempt is refused, like a stopped server."""
    engine = create_engine(DOWN_DB_URL, creator=_refuse_connection)
    yield engine
    engine.dispose()


@pytest.fixture
def down_client(down_engine: Engine) -> Generator[TestClient, None, None]:
    """Request-scoped sessions point at the down database. The lifespan is not run, and the
    default raise_server_exceptions=True makes the test fail if an error reaches Starlette's
    ServerErrorMiddleware instead of being answered by a specific exception handler."""
    down_session = sessionmaker(bind=down_engine, autoflush=False)

    def override_get_db() -> Generator[Session, None, None]:
        db = down_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_a_request_with_the_db_down_answers_503_without_driver_text(
    down_client: TestClient,
) -> None:
    response = down_client.get(PROBE_PATH)

    assert response.status_code == 503
    assert response.json() == DOWN_BODY
    assert SECRET not in response.text


@pytest.fixture
def schemaless_client() -> Generator[TestClient, None, None]:
    """The database answers, but has none of the app's tables (schema drift)."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    empty_session = sessionmaker(bind=engine, autoflush=False)

    def override_get_db() -> Generator[Session, None, None]:
        db = empty_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()
    engine.dispose()


def test_a_schema_bug_is_a_500_with_a_fixed_body_not_an_outage(
    schemaless_client: TestClient,
) -> None:
    response = schemaless_client.get(PROBE_PATH)

    assert response.status_code == 500
    assert response.json() == {"error": {"code": "INTERNAL_ERROR", "message": "Server error"}}
    assert "no such table" not in response.text


def test_startup_logs_one_line_without_password_and_keeps_serving_with_hat_off(
    down_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(database, "engine", down_engine)
    monkeypatch.delenv("HAT_ENABLED", raising=False)

    with caplog.at_level(logging.INFO):
        asyncio.run(app_main._init_database())

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r.getMessage() for r in errors] == [
        "[ERROR] Database unreachable at db.invalid:5432/algotrading (start Postgres)"
    ]
    assert errors[0].exc_info is None
    assert SECRET not in caplog.text


def test_startup_with_hat_enabled_refuses_a_down_database(
    down_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(database, "engine", down_engine)
    monkeypatch.setenv("HAT_ENABLED", "true")

    with pytest.raises(DatabaseUnavailableError):
        asyncio.run(app_main._init_database())


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_connection_refused(), True),
        (OperationalError("x", {}, sqlite3.OperationalError("database is locked")), True),
        (OperationalError("x", {}, sqlite3.OperationalError("no such table: t")), False),
        (ProgrammingError("x", {}, Exception("syntax error")), False),
        (IntegrityError("x", {}, Exception("duplicate key")), False),
        (RuntimeError("boom"), False),
    ],
)
def test_is_db_unavailable_matches_connection_loss_only(exc: BaseException, expected: bool) -> None:
    assert is_db_unavailable(exc) is expected


def test_is_db_unavailable_follows_the_cause_chain() -> None:
    try:
        try:
            raise _connection_refused()
        except OperationalError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert is_db_unavailable(outer) is True


def test_log_filter_drops_traceback_and_throttles_per_site() -> None:
    lines: list[str] = []

    class ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    handler = ListHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(DatabaseUnavailableLogFilter())
    log = logging.getLogger("test_db_unavailable.filter")
    log.propagate = False
    log.addHandler(handler)
    try:
        for _ in range(3):
            try:
                raise _connection_refused()
            except OperationalError as exc:
                log.exception("Failed to load: %s", exc)
        try:
            raise ValueError("unrelated")
        except ValueError:
            log.exception("Other failure")
    finally:
        log.removeHandler(handler)

    assert lines[0] == (
        "ERROR Failed to load: (builtins.Exception) connection refused hunter2-secret "
        "[database unavailable; traceback suppressed]"
    )
    assert len(lines) == 2
    assert lines[1].startswith("ERROR Other failure\nTraceback (most recent call last):")
