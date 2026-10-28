"""Postgres fixtures for the HAT database checks.

These run only with SMARTTRADER_PG_TESTS=1 and DB_* pointing at a throwaway database whose name
ends in `_test` (CI's `backend-postgres` job). The HAT tables of both databases (hat_live's and
hat_state's, whose names never collide) are dropped and recreated in it once per session; every
check then runs inside a transaction that is rolled back, with a fresh in-memory config store
(`hat_db_builders.STORE`).
"""

import os
from collections.abc import Generator

import hat_db_builders as b
import pytest
from hat_db_rules import ensure_both_schemas
from sqlalchemy import Connection, Engine, create_engine, text

from app.base import Base, StateBase
from app.database import postgres_url_from_env
from app.hat import models  # noqa: F401  (registers the hat_* tables)
from app.utils.env_loader import env_flag


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if env_flag("SMARTTRADER_PG_TESTS"):
        return
    skip = pytest.mark.skip(reason="set SMARTTRADER_PG_TESTS=1 to run")
    for item in items:
        if "tests/hat/db/" in str(item.path).replace("\\", "/"):
            item.add_marker(skip)


# A check whose lock wait never ends fails after this instead of hanging the CI job (C26). The
# lock-wait probes give a waiter up to 5 s before they release the holder: well inside it.
LOCK_TIMEOUT = "-c lock_timeout=15s"


def hat_tables() -> list:
    tables = {**Base.metadata.tables, **StateBase.metadata.tables}
    return [table for name, table in tables.items() if name.startswith("hat_")]


def drop_hat_objects(engine: Engine) -> None:
    with engine.begin() as connection:
        names = connection.execute(
            text("SELECT tablename FROM pg_tables WHERE tablename LIKE 'hat\\_%' ESCAPE '\\'")
        ).scalars()
        for name in list(names):
            connection.execute(text(f'DROP TABLE IF EXISTS "{name}" CASCADE'))
        for function in (
            "hat_stop_only_tightens",
            "hat_position_never_deleted",
            "hat_signal_confidence_immutable",
            "hat_event_is_append_only",
            "hat_corporate_action_version_is_append_only",
            "hat_financial_result_is_append_only",
            "hat_strategy_version_immutable",  # dropped from the schema; kept for old databases
            "hat_universe_version_immutable",
            "hat_risk_policy_version_immutable",  # dropped from the schema; kept for old ones
        ):
            connection.execute(text(f"DROP FUNCTION IF EXISTS {function}() CASCADE"))


@pytest.fixture(scope="session")
def pg_engine() -> Generator[Engine, None, None]:
    database = os.getenv("DB_NAME", "")
    if not database.endswith("_test"):
        pytest.fail(f"refusing to drop tables in DB_NAME={database!r}; use a *_test database")
    if os.getenv("DB_STATE_NAME") != database:  # `init_db` reaches hat_state through it
        pytest.fail("set DB_STATE_NAME to DB_NAME: both databases are the one test database")
    engine = create_engine(postgres_url_from_env(), connect_args={"options": LOCK_TIMEOUT})
    drop_hat_objects(engine)
    Base.metadata.create_all(
        engine, tables=[t for t in hat_tables() if t.metadata is Base.metadata]
    )
    StateBase.metadata.create_all(
        engine, tables=[t for t in hat_tables() if t.metadata is StateBase.metadata]
    )
    ensure_both_schemas(engine)
    yield engine
    drop_hat_objects(engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def config_store() -> None:
    """Each check's own config store (`hat_db_builders.STORE`)."""
    b.reset_store()


@pytest.fixture
def conn(pg_engine: Engine) -> Generator[Connection, None, None]:
    """A connection inside a transaction that is rolled back after the check."""
    with pg_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()
