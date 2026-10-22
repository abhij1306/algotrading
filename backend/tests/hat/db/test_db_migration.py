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

    # `init_db()` runs the HAT migration: with every trigger and function removed first, and the
    # state CHECKs a table created before PR 11 lacks, one run must restore the catalog exactly
    # (a disabled HAT swallows upkeep errors, so the restored catalog is what proves the step
    # ran; `create_all` never adds a constraint to an existing table).
    before = rules.catalog(pg_engine)
    with pg_engine.begin() as connection:
        for table in (
            "hat_strategy_version",
            "hat_universe_version",
            "hat_risk_policy_version",
            "hat_position",
            "hat_event",
        ):
            names = connection.execute(
                text(
                    "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"
                    " AND tgrelid = CAST(:t AS regclass)"
                ),
                {"t": table},
            ).scalars()
            for name in list(names):
                connection.execute(text(f"DROP TRIGGER {name} ON {table}"))
        connection.execute(text("DROP FUNCTION IF EXISTS hat_strategy_version_immutable CASCADE"))
        connection.execute(text("DROP FUNCTION IF EXISTS hat_universe_version_immutable CASCADE"))
        connection.execute(text("DROP FUNCTION IF EXISTS hat_stop_only_tightens CASCADE"))
        connection.execute(text("DROP FUNCTION IF EXISTS hat_event_is_append_only CASCADE"))
        connection.execute(
            text("DROP FUNCTION IF EXISTS hat_risk_policy_version_immutable CASCADE")
        )
        connection.execute(text("ALTER TABLE hat_signal DROP CONSTRAINT hat_signal_state_check"))
        connection.execute(
            text("ALTER TABLE hat_position DROP CONSTRAINT hat_position_state_check")
        )
    assert rules.catalog(pg_engine)["triggers"] != before["triggers"]
    assert rules.catalog(pg_engine)["constraints"] != before["constraints"]
    init_db()
    assert rules.catalog(pg_engine) == before

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
            # A database from before PR 45: bots defaulted to shadow and no DDPI columns.
            connection.execute(
                text("ALTER TABLE hat_bot ALTER COLUMN execution_mode SET DEFAULT 'shadow'")
            )
            connection.execute(text("ALTER TABLE hat_broker_account DROP COLUMN ddpi_confirmed"))
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
                        " WHERE table_name IN ('hat_instrument', 'hat_job_run',"
                        " 'hat_broker_account')"
                    )
                )
            }
            default = connection.execute(
                text(
                    "SELECT column_default FROM information_schema.columns"
                    " WHERE table_name = 'hat_bot' AND column_name = 'execution_mode'"
                )
            ).scalar_one()
            kept = connection.execute(text("SELECT calls FROM hat_job_run WHERE job = 'old'"))
            assert {"delisted_on", "rows", "ddpi_confirmed"} <= columns
            assert default is not None
            assert default.startswith("'confirm'")
            assert kept.scalar_one() == 3
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM hat_job_run WHERE job = 'old'"))
        engine.dispose()
