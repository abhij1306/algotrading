"""ensure_universe_schema's custom_universe_members FK repair against a real Postgres.

Runs only with SMARTTRADER_PG_TESTS=1 and DB_* pointing at a throwaway database whose name
ends in ``_test`` (CI's backend-postgres job). The tests drop and recreate the universe
tables, so they refuse any other database.
"""

import os
import threading
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.base import Base
from app.database import ensure_universe_schema, get_db, postgres_url_from_env
from app.main import app
from app.models.universe import CustomUniverse, CustomUniverseMember, IndexUniverseDefinition
from app.utils.env_loader import env_flag

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not env_flag("SMARTTRADER_PG_TESTS"), reason="set SMARTTRADER_PG_TESTS=1 to run"
    ),
]

_TABLES = ("custom_universe_members", "custom_universes", "index_universe_definitions")
_MEMBERS_SQL = "SELECT universe_id, symbol FROM custom_universe_members ORDER BY symbol"
_INDEX_SQL = "SELECT id, index_code FROM index_universe_definitions ORDER BY id"


def _new_engine() -> Engine:
    return create_engine(postgres_url_from_env())


def _drop_tables(engine: Engine) -> None:
    with engine.begin() as conn:
        for table in _TABLES:
            conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))


@pytest.fixture
def pg_engine() -> Generator[Engine, None, None]:
    database = os.getenv("DB_NAME", "")
    if not database.endswith("_test"):
        pytest.fail(f"refusing to drop tables in DB_NAME={database!r}; use a *_test database")
    engine = _new_engine()
    _drop_tables(engine)
    yield engine
    _drop_tables(engine)
    engine.dispose()


def _create_legacy_schema(engine: Engine) -> None:
    """The schema main used to create: members.universe_id -> index_universe_definitions.id."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE index_universe_definitions "
                "(id SERIAL PRIMARY KEY, index_code VARCHAR(50) NOT NULL UNIQUE)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE custom_universes (id SERIAL PRIMARY KEY, "
                "universe_code VARCHAR(20) NOT NULL UNIQUE, universe_name VARCHAR(100) NOT NULL, "
                "description TEXT, criteria TEXT, created_by VARCHAR(50), is_active BOOLEAN, "
                "created_at TIMESTAMP DEFAULT now(), updated_at TIMESTAMP DEFAULT now())"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE custom_universe_members (id SERIAL PRIMARY KEY, "
                "universe_id INTEGER NOT NULL REFERENCES index_universe_definitions (id), "
                "symbol VARCHAR(20) NOT NULL, weight DOUBLE PRECISION, "
                "added_at TIMESTAMP DEFAULT now(), "
                "CONSTRAINT uq_custom_universe_member UNIQUE (universe_id, symbol))"
            )
        )
        conn.execute(
            text(
                "INSERT INTO index_universe_definitions (id, index_code) "
                "VALUES (1, 'NIFTY50'), (7, 'BANKNIFTY')"
            )
        )
        conn.execute(
            text("INSERT INTO custom_universes (universe_code, universe_name) VALUES ('MINE', 'M')")
        )
        # Universe 1 is both an index definition and a custom universe, the only way a member
        # could be written under the wrong FK. 7 is an index definition only: an orphan.
        conn.execute(
            text(
                "INSERT INTO custom_universe_members (universe_id, symbol) "
                "VALUES (1, 'SBIN'), (1, 'TCS'), (7, 'HDFCBANK')"
            )
        )


def _member_fks(engine: Engine) -> list[tuple[str | None, str, list[str]]]:
    return [
        (fk["name"], fk["referred_table"], fk["referred_columns"])
        for fk in inspect(engine).get_foreign_keys("custom_universe_members")
        if fk["constrained_columns"] == ["universe_id"]
    ]


def _rows(engine: Engine, sql: str) -> list[tuple]:
    with engine.connect() as conn:
        return [tuple(row) for row in conn.execute(text(sql))]


_FIXED_FK = [("custom_universe_members_universe_id_fkey", "custom_universes", ["id"])]


def test_migration_repoints_the_fk_drops_orphans_and_reruns_as_a_no_op(pg_engine: Engine) -> None:
    _create_legacy_schema(pg_engine)
    assert _member_fks(pg_engine) == [
        ("custom_universe_members_universe_id_fkey", "index_universe_definitions", ["id"])
    ]

    ensure_universe_schema(bind=pg_engine)

    assert _member_fks(pg_engine) == _FIXED_FK
    assert _rows(pg_engine, _MEMBERS_SQL) == [(1, "SBIN"), (1, "TCS")]
    assert _rows(pg_engine, _INDEX_SQL) == [(1, "NIFTY50"), (7, "BANKNIFTY")]

    ensure_universe_schema(bind=pg_engine)

    assert _member_fks(pg_engine) == _FIXED_FK
    assert _rows(pg_engine, _MEMBERS_SQL) == [(1, "SBIN"), (1, "TCS")]
    assert _rows(pg_engine, _INDEX_SQL) == [(1, "NIFTY50"), (7, "BANKNIFTY")]
    with pytest.raises(IntegrityError), pg_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO custom_universe_members (universe_id, symbol) VALUES (7, 'X')")
        )


def test_concurrent_starts_apply_the_migration_once(pg_engine: Engine) -> None:
    _create_legacy_schema(pg_engine)
    engines = [_new_engine(), _new_engine()]
    barrier = threading.Barrier(len(engines))
    errors: list[BaseException] = []

    def start(engine: Engine) -> None:
        try:
            barrier.wait(timeout=30)
            ensure_universe_schema(bind=engine)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=start, args=(engine,)) for engine in engines]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    for engine in engines:
        engine.dispose()

    assert [thread.is_alive() for thread in threads] == [False, False]
    assert errors == []
    assert _member_fks(pg_engine) == _FIXED_FK
    assert _rows(pg_engine, _MEMBERS_SQL) == [(1, "SBIN"), (1, "TCS")]
    assert _rows(pg_engine, _INDEX_SQL) == [(1, "NIFTY50"), (7, "BANKNIFTY")]


def test_migration_is_a_no_op_on_a_fresh_database(pg_engine: Engine) -> None:
    Base.metadata.create_all(
        pg_engine,
        tables=[
            IndexUniverseDefinition.__table__,
            CustomUniverse.__table__,
            CustomUniverseMember.__table__,
        ],
    )

    ensure_universe_schema(bind=pg_engine)

    assert _member_fks(pg_engine) == _FIXED_FK


def test_post_then_get_on_a_migrated_legacy_database(pg_engine: Engine) -> None:
    _create_legacy_schema(pg_engine)
    ensure_universe_schema(bind=pg_engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=pg_engine)

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

    assert created.status_code == 200
    assert created.json()["symbol_count"] == 2
    assert fetched.status_code == 200
    assert sorted(fetched.json()["constituents"]) == ["INFY", "TCS"]
