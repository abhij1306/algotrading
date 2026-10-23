"""DB checks for the migration mechanism."""

import uuid
from datetime import UTC, datetime, timedelta

import hat_db_rules as rules
import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.database import init_db
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
