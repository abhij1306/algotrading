"""Custom universes: the service in services/universe/custom.py, the /api/universe/custom
routes and the ensure_universe_schema FK migration, against a throwaway SQLite file with
foreign keys enforced."""

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import (
    Column,
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.base import Base
from app.database import ensure_universe_schema, get_db
from app.main import app
from app.models.universe import CustomUniverse, CustomUniverseMember
from app.services.universe.custom import (
    DuplicateCustomUniverseError,
    create_custom_universe,
    get_custom_universe_symbols,
)


def _sqlite_engine(path: Path) -> Engine:
    """A SQLite engine that enforces foreign keys, as Postgres does."""
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection: sqlite3.Connection, _record: object) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    return engine


@pytest.fixture
def session_factory(tmp_path: Path) -> Generator[sessionmaker, None, None]:
    engine = _sqlite_engine(tmp_path / "cuni.db")
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


# --- schema migration ------------------------------------------------------------------


def _member_fk_targets(engine: Engine) -> list[tuple[str, list[str]]]:
    return [
        (fk["referred_table"], fk["referred_columns"])
        for fk in inspect(engine).get_foreign_keys("custom_universe_members")
        if fk["constrained_columns"] == ["universe_id"]
    ]


def _legacy_engine(path: Path) -> Engine:
    """A database as main left it: members.universe_id -> index_universe_definitions.id."""
    engine = _sqlite_engine(path)
    legacy = MetaData()
    Table(
        "index_universe_definitions",
        legacy,
        Column("id", Integer, primary_key=True),
        Column("index_code", String(50)),
    )
    CustomUniverse.__table__.to_metadata(legacy)
    Table(
        "custom_universe_members",
        legacy,
        Column("id", Integer, primary_key=True),
        Column("universe_id", Integer, ForeignKey("index_universe_definitions.id"), nullable=False),
        Column("symbol", String(20), nullable=False, index=True),
        Column("weight", Float),
        Column("added_at", DateTime),
    )
    legacy.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO index_universe_definitions VALUES (1, 'NIFTY50')"))
        conn.execute(text("INSERT INTO index_universe_definitions VALUES (7, 'BANKNIFTY')"))
        conn.execute(
            text(
                "INSERT INTO custom_universes (id, universe_code, universe_name) "
                "VALUES (1, 'MINE', 'Mine')"
            )
        )
        # id 1 is both an index definition and a custom universe: the only way a member
        # could be written under the wrong FK. id 7 is an index definition only (orphan).
        conn.execute(
            text(
                "INSERT INTO custom_universe_members (id, universe_id, symbol, weight) "
                "VALUES (1, 1, 'SBIN', 0.5), (2, 1, 'TCS', NULL), (3, 7, 'HDFCBANK', NULL)"
            )
        )
    return engine


def _rows(engine: Engine, sql: str) -> list[tuple]:
    with engine.connect() as conn:
        return [tuple(row) for row in conn.execute(text(sql))]


def test_model_fk_targets_custom_universes() -> None:
    assert [
        fk.target_fullname for fk in CustomUniverseMember.__table__.c.universe_id.foreign_keys
    ] == ["custom_universes.id"]


def test_migration_repoints_the_fk_drops_orphans_and_reruns_as_a_no_op(tmp_path: Path) -> None:
    engine = _legacy_engine(tmp_path / "legacy.db")
    assert _member_fk_targets(engine) == [("index_universe_definitions", ["id"])]

    ensure_universe_schema(bind=engine)

    assert _member_fk_targets(engine) == [("custom_universes", ["id"])]
    members_sql = "SELECT id, universe_id, symbol, weight FROM custom_universe_members ORDER BY id"
    assert _rows(engine, members_sql) == [(1, 1, "SBIN", 0.5), (2, 1, "TCS", None)]
    assert _rows(engine, "SELECT id, index_code FROM index_universe_definitions ORDER BY id") == [
        (1, "NIFTY50"),
        (7, "BANKNIFTY"),
    ]
    assert sorted(i["name"] for i in inspect(engine).get_indexes("custom_universe_members")) == [
        "idx_custom_universe_symbol",
        "ix_custom_universe_members_symbol",
    ]

    ensure_universe_schema(bind=engine)

    assert _member_fk_targets(engine) == [("custom_universes", ["id"])]
    assert _rows(engine, members_sql) == [(1, 1, "SBIN", 0.5), (2, 1, "TCS", None)]
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text("INSERT INTO custom_universe_members (universe_id, symbol) VALUES (7, 'X')")
        )
    engine.dispose()


def test_migration_is_a_no_op_on_a_fresh_or_empty_database(tmp_path: Path) -> None:
    empty = _sqlite_engine(tmp_path / "empty.db")
    ensure_universe_schema(bind=empty)
    assert inspect(empty).get_table_names() == []
    empty.dispose()

    fresh = _sqlite_engine(tmp_path / "fresh.db")
    Base.metadata.create_all(fresh)
    ensure_universe_schema(bind=fresh)
    assert _member_fk_targets(fresh) == [("custom_universes", ["id"])]
    fresh.dispose()


def test_post_then_get_works_on_a_migrated_legacy_database(tmp_path: Path) -> None:
    engine = _legacy_engine(tmp_path / "legacy_api.db")
    ensure_universe_schema(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        created = client.post(
            "/api/universe/custom",
            json={"universe_code": "IT", "name": "IT names", "symbols": ["TCS", "INFY"]},
        )
        fetched = client.get("/api/universe/custom/IT")
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()

    assert created.status_code == 200
    assert sorted(fetched.json()["constituents"]) == ["INFY", "TCS"]
