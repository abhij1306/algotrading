"""Postgres fixtures for the HAT database checks (HAT-VER-050).

These run only with SMARTTRADER_PG_TESTS=1 and DB_* pointing at a throwaway database whose name
ends in `_test` (CI's `backend-postgres` job). The HAT tables are dropped and recreated once per
session; every check then runs inside a transaction that is rolled back.
"""

import os
from collections.abc import Generator

import pytest
from sqlalchemy import Connection, Engine, create_engine, text

from app.base import Base
from app.database import postgres_url_from_env
from app.hat import models  # noqa: F401  (registers the hat_* tables)
from app.hat.schema import ensure_hat_schema
from app.utils.env_loader import env_flag


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if env_flag("SMARTTRADER_PG_TESTS"):
        return
    skip = pytest.mark.skip(reason="set SMARTTRADER_PG_TESTS=1 to run")
    for item in items:
        if "tests/hat/db/" in str(item.path).replace("\\", "/"):
            item.add_marker(skip)


def hat_tables() -> list:
    return [table for name, table in Base.metadata.tables.items() if name.startswith("hat_")]


def drop_hat_objects(engine: Engine) -> None:
    with engine.begin() as connection:
        names = connection.execute(
            text("SELECT tablename FROM pg_tables WHERE tablename LIKE 'hat\\_%' ESCAPE '\\'")
        ).scalars()
        for name in list(names):
            connection.execute(text(f'DROP TABLE IF EXISTS "{name}" CASCADE'))
        for function in (
            "hat_stop_only_tightens",
            "hat_event_is_append_only",
            "hat_strategy_version_immutable",
            "hat_universe_version_immutable",
            "hat_risk_policy_version_immutable",
        ):
            connection.execute(text(f"DROP FUNCTION IF EXISTS {function}() CASCADE"))


@pytest.fixture(scope="session")
def pg_engine() -> Generator[Engine, None, None]:
    database = os.getenv("DB_NAME", "")
    if not database.endswith("_test"):
        pytest.fail(f"refusing to drop tables in DB_NAME={database!r}; use a *_test database")
    engine = create_engine(postgres_url_from_env())
    drop_hat_objects(engine)
    Base.metadata.create_all(engine, tables=hat_tables())
    ensure_hat_schema(engine)
    yield engine
    drop_hat_objects(engine)
    engine.dispose()


@pytest.fixture
def conn(pg_engine: Engine) -> Generator[Connection, None, None]:
    """A connection inside a transaction that is rolled back after the check."""
    with pg_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()
