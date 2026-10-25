"""DB checks for the migration mechanism."""

import uuid
from datetime import UTC, datetime, timedelta

import hat_db_builders as b
import hat_db_rules as rules
import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.database import init_db
from app.hat.confighash import config_hash
from app.hat.models import HatRiskPolicyVersion
from app.hat.risk.policy import OLD_SEED_POLICY, SEED_POLICY
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
            "hat_signal",
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
        connection.execute(text("DROP FUNCTION IF EXISTS hat_signal_confidence_immutable CASCADE"))
        connection.execute(text("ALTER TABLE hat_signal DROP CONSTRAINT hat_signal_state_check"))
        connection.execute(
            text("ALTER TABLE hat_signal DROP CONSTRAINT hat_signal_confidence_check")
        )
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

    # An older policy: a user still on the PR 10 seed is moved to the current seed.
    _old_seed_superseded(pg_engine)


def _old_seed_superseded(pg_engine: Engine) -> None:
    """: `init_db()` moves a user still on the PR 10 seed to the current seed as
    an `initial` version: the immutability trigger lets the old row be superseded, the cooling-off
    CHECK accepts the new one, and a second run writes nothing."""
    user = uuid.uuid4()
    past = datetime.now(UTC) - timedelta(days=30)
    with Session(pg_engine) as session:
        session.add(
            HatRiskPolicyVersion(
                user_id=user,
                version=1,
                policy=dict(OLD_SEED_POLICY),
                direction="initial",
                requested_at=past,
                effective_from=past,
            )
        )
        session.commit()
    # Policy versions cannot be deleted; the user is fresh and the session fixture drops the
    # HAT tables at the end, so nothing leaks into other checks.
    init_db()
    init_db()
    with Session(pg_engine) as session:
        rows = session.scalars(
            select(HatRiskPolicyVersion)
            .where(HatRiskPolicyVersion.user_id == user)
            .order_by(HatRiskPolicyVersion.version)
        ).all()
        assert [(r.version, r.direction, r.policy == SEED_POLICY) for r in rows] == [
            (1, "initial", False),
            (2, "initial", True),
        ]
        assert rows[0].superseded_at == rows[1].effective_from


_OLD_VERSION_SCHEMA = (
    "ALTER TABLE hat_strategy_version DROP CONSTRAINT hat_strategy_version_status_check",
    "ALTER TABLE hat_strategy_version DROP CONSTRAINT hat_strategy_version_release_evidence_check",
    "DROP INDEX hat_one_released_version",
    "ALTER TABLE hat_strategy_version ADD CONSTRAINT ck_hat_strategy_version_status"
    " CHECK (status IN ('draft', 'backtested', 'shadow', 'live', 'retired'))",
    "ALTER TABLE hat_strategy_version ADD CONSTRAINT ck_hat_strategy_version_live_evidence"
    " CHECK (status <> 'live' OR (backtest_run_id IS NOT NULL AND shadow_started_at IS NOT NULL))",
    "CREATE UNIQUE INDEX hat_one_live_version ON hat_strategy_version (strategy_id)"
    " WHERE status = 'live'",
)


@pytest.mark.postgres
def test_migration__version_states_renamed(pg_engine: Engine) -> None:
    """A database with the old version states and exits carrying the unread `when` (hat_live)
    comes out of `ensure_hat_schema` with the new states, the models' catalog, no `when` and each
    hash recomputed from its config, its immutability trigger on again; a second run changes
    nothing."""
    before = rules.catalog(pg_engine)
    with pg_engine.connect() as connection:
        transaction = connection.begin()
        try:
            for statement in _OLD_VERSION_SCHEMA:
                connection.execute(text(statement))
            market = b.market(connection)
            strategy = b.strategy(connection, market)
            run = b.backtest_run(
                connection, b.strategy_version(connection, strategy, 9), b.risk_policy(connection)
            )
            exits = [{"rule": "close_below_sma", "sma": 50, "when": "15:25"}, {"rule": "hard_stop"}]
            config = {"bot": "s", "exits": exits}
            ids = {}
            for number, status in enumerate(("draft", "backtested", "shadow", "live", "retired")):
                evidence = {"backtest_run_id": run, "shadow_started_at": b.NOW}
                ids[status] = b.strategy_version(
                    connection,
                    strategy,
                    number + 1,
                    status=status,
                    config=config,
                    config_hash=config_hash(config),
                    **(evidence if status == "live" else {}),
                )
            ensure_hat_schema(connection)
            ensure_hat_schema(connection)
            stripped = {"bot": "s", "exits": [{"rule": "close_below_sma", "sma": 50}, exits[1]]}
            rows = {
                r.id: r
                for r in connection.execute(
                    text("SELECT id, status, config, config_hash FROM hat_strategy_version")
                )
            }
            renamed = {"draft": "draft", "backtested": "frozen", "shadow": "trial"}
            renamed |= {"live": "released", "retired": "superseded"}
            for old, new in renamed.items():
                row = rows[ids[old]]
                assert (row.status, row.config, row.config_hash) == (
                    new,
                    stripped,
                    config_hash(stripped),
                )
            enabled = connection.execute(
                text(
                    "SELECT tgenabled FROM pg_trigger"
                    " WHERE tgname = 'hat_strategy_version_immutable_guard'"
                )
            ).scalar_one()
            assert enabled == "O"
            transaction.commit()
            assert rules.catalog(pg_engine) == before
        finally:
            if transaction.is_active:
                transaction.rollback()
