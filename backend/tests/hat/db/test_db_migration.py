"""DB checks for the migration mechanism (HAT-SCH-101, HAT-SCH-103)."""

import hat_db_rules as rules
import pytest
from sqlalchemy import Engine, create_engine, text

from app.database import init_db, postgres_url_from_env
from app.hat.schema import ensure_hat_schema

pytestmark = pytest.mark.postgres


@pytest.mark.postgres
def test_migration__ensure_idempotent(pg_engine: Engine) -> None:
    # The Postgres-only DDL, repeated.
    rules.check_migration__ensure_idempotent(pg_engine, ensure_hat_schema)

    # `init_db()` end to end: any number of runs gives the catalog of one run, one NSE row.
    # (init_db uses the process engine, which points at the same DB_* database.)
    rules.check_migration__ensure_idempotent(pg_engine, lambda _engine: init_db())
    with pg_engine.connect() as connection:
        count = connection.execute(text("SELECT count(*) FROM hat_market WHERE code = 'NSE'"))
        assert count.scalar_one() == 1

    # An older database: columns the models have since gained are added back, rows kept.
    engine = create_engine(postgres_url_from_env())
    try:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE hat_instrument DROP COLUMN delisted_on"))
            connection.execute(text("ALTER TABLE hat_job_run DROP COLUMN rows"))
            connection.execute(
                text(
                    "INSERT INTO hat_job_run (id, job, trade_date, started_at, status, calls)"
                    " VALUES (gen_random_uuid(), 'old', current_date, now(), 'failed', 3)"
                )
            )
        init_db()
        init_db()
        with engine.connect() as connection:
            columns = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name IN ('hat_instrument', 'hat_job_run')"
                    )
                )
            }
            kept = connection.execute(text("SELECT calls FROM hat_job_run WHERE job = 'old'"))
            assert {"delisted_on", "rows"} <= columns
            assert kept.scalar_one() == 3
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM hat_job_run WHERE job = 'old'"))
        engine.dispose()
