"""Custom universes: the service in services/universe/custom.py and the /api/universe/custom
routes, against a throwaway SQLite file."""

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.base import Base
from app.database import get_db
from app.main import app
from app.models.universe import CustomUniverse, CustomUniverseMember
from app.services.universe.custom import (
    DuplicateCustomUniverseError,
    create_custom_universe,
    get_custom_universe_symbols,
)


@pytest.fixture
def session_factory(tmp_path: Path) -> Generator[sessionmaker, None, None]:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cuni.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[CustomUniverse.__table__, CustomUniverseMember.__table__],
    )
    yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
    engine.dispose()


@pytest.fixture
def db(session_factory: sessionmaker) -> Generator[Session, None, None]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def api(session_factory: sessionmaker) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


# --- service ---------------------------------------------------------------------------


def test_create_then_read_back_in_a_new_session(db: Session, session_factory: sessionmaker) -> None:
    created = create_custom_universe(
        db, "BANKS", "My banks", ["SBIN", "HDFCBANK"], description="d", created_by="me"
    )
    db.commit()

    with session_factory() as fresh:
        row = fresh.query(CustomUniverse).filter_by(universe_code="BANKS").one()
        assert (row.id, row.universe_name, row.description, row.created_by, row.is_active) == (
            created.id,
            "My banks",
            "d",
            "me",
            True,
        )
        assert sorted(get_custom_universe_symbols(fresh, "BANKS") or []) == ["HDFCBANK", "SBIN"]


def test_create_defaults_and_stores_symbols_as_given(db: Session) -> None:
    create_custom_universe(db, "RAW", "Raw", ["NSE:SBIN-EQ", "tcs"])
    db.commit()

    row = db.query(CustomUniverse).filter_by(universe_code="RAW").one()
    assert (row.description, row.created_by) == ("", "system")
    assert sorted(get_custom_universe_symbols(db, "RAW") or []) == ["NSE:SBIN-EQ", "tcs"]


def test_create_with_no_symbols_gives_an_empty_universe(db: Session) -> None:
    create_custom_universe(db, "EMPTY", "Empty", [])
    db.commit()

    assert get_custom_universe_symbols(db, "EMPTY") == []


def test_unknown_code_returns_none(db: Session) -> None:
    assert get_custom_universe_symbols(db, "NOPE") is None


def test_duplicate_code_raises_the_fixed_message(db: Session) -> None:
    first = create_custom_universe(db, "DUP", "First", ["SBIN"])
    db.commit()

    with pytest.raises(DuplicateCustomUniverseError) as excinfo:
        create_custom_universe(db, "DUP", "Second", ["TCS"])

    assert str(excinfo.value) == f"Custom universe with code 'DUP' already exists (ID: {first.id})"
    db.rollback()
    assert get_custom_universe_symbols(db, "DUP") == ["SBIN"]


def test_create_leaves_the_commit_to_the_caller(db: Session, session_factory: sessionmaker) -> None:
    create_custom_universe(db, "PENDING", "Pending", ["SBIN"])
    db.rollback()

    with session_factory() as fresh:
        assert get_custom_universe_symbols(fresh, "PENDING") is None


# --- routes ----------------------------------------------------------------------------


def test_post_then_get_custom_universe(api: TestClient) -> None:
    created = api.post(
        "/api/universe/custom",
        json={"universe_code": "IT", "name": "IT names", "symbols": ["TCS", "INFY"]},
    )
    assert created.status_code == 200
    assert created.json() == {
        "status": "success",
        "message": "Custom universe 'IT' created",
        "universe_code": "IT",
        "name": "IT names",
        "symbol_count": 2,
    }

    fetched = api.get("/api/universe/custom/IT")
    assert fetched.status_code == 200
    body = fetched.json()
    assert sorted(body.pop("constituents")) == ["INFY", "TCS"]
    assert body == {"universe_code": "IT", "count": 2, "source": "custom_universe"}


def test_post_duplicate_code_is_a_400_with_the_fixed_message(api: TestClient) -> None:
    payload = {"universe_code": "DUP", "name": "Dup", "symbols": ["SBIN"]}
    assert api.post("/api/universe/custom", json=payload).status_code == 200

    again = api.post("/api/universe/custom", json=payload)

    assert again.status_code == 400
    assert again.json()["detail"] == "Custom universe with code 'DUP' already exists (ID: 1)"


def test_get_unknown_custom_universe_is_a_404(api: TestClient) -> None:
    response = api.get("/api/universe/custom/MISSING")

    assert response.status_code == 404
    assert response.json()["detail"] == "Custom universe 'MISSING' not found"
