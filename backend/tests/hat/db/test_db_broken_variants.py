"""Every DB check must fail when the rule it checks is removed (HAT-SCH-102, HAT-VER-032).

Each variant removes or weakens one trigger, index or constraint inside a transaction (Postgres
DDL is transactional, so nothing persists) and requires the matching check to raise
AssertionError. A check that still passes is vacuous.
"""

from collections.abc import Callable

import hat_db_rules as rules
import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from app.hat.schema import PG_DDL

pytestmark = pytest.mark.postgres

Break = Callable[[Connection], None]


def sql(*statements: str) -> Break:
    def apply(conn: Connection) -> None:
        for statement in statements:
            conn.execute(text(statement))

    return apply


def drop_constraint(table: str, name: str) -> str:
    return f"ALTER TABLE {table} DROP CONSTRAINT {name}"


def drop_keyed(table: str, kind: str, columns: tuple[str, ...]) -> Break:
    """Drop the unique (`u`) or primary key (`p`) constraint of `table` on exactly `columns`."""

    def apply(conn: Connection) -> None:
        name = conn.execute(
            text(
                "SELECT c.conname FROM pg_constraint c WHERE c.conrelid = CAST(:t AS regclass)"
                " AND c.contype = :k AND (SELECT array_agg(a.attname::text ORDER BY a.attname)"
                " FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey))"
                " = CAST(:cols AS text[])"
            ),
            {"t": table, "k": kind, "cols": sorted(columns)},
        ).scalar_one()
        conn.execute(text(drop_constraint(table, name)))

    return apply


# Each entry: check name -> list of (label, how to break it).
VARIANTS: dict[str, list[tuple[str, Break]]] = {
    "reference.instrument_unique": [
        (
            "unique (market_id, isin, series) dropped",
            drop_keyed("hat_instrument", "u", ("market_id", "isin", "series")),
        )
    ],
    "reference.broker_symbol_unique": [
        (
            "unique (broker, broker_symbol) dropped",
            drop_keyed("hat_broker_symbol", "u", ("broker", "broker_symbol")),
        )
    ],
    "reference.corporate_action_unique": [
        (
            "unique (instrument_id, ex_date, action_type) dropped",
            drop_keyed("hat_corporate_action", "u", ("instrument_id", "ex_date", "action_type")),
        )
    ],
    "reference.announcement_guid_unique": [
        ("unique (guid) dropped", drop_keyed("hat_announcement", "u", ("guid",)))
    ],
    "reference.band_pct_check": [
        ("band CHECK dropped", sql(drop_constraint("hat_price_band", "ck_hat_price_band_pct")))
    ],
    "governance.domain_checks": [
        (f"{name} dropped", sql(drop_constraint(table, name)))
        for table, name in (
            ("hat_strategy_version", "ck_hat_strategy_version_status"),
            ("hat_risk_policy_version", "ck_hat_risk_policy_direction"),
            ("hat_bot", "ck_hat_bot_execution_mode"),
            ("hat_bot", "ck_hat_bot_status"),
            ("hat_bot", "ck_hat_bot_lifecycle_state"),
            ("hat_kill_switch", "ck_hat_kill_switch_state"),
            ("hat_exposure_state", "ck_hat_exposure_state_level"),
            ("hat_criterion_type", "ck_hat_criterion_type_stage"),
        )
    ]
    + [
        (
            "kill switch scope and scope/bot CHECKs dropped",
            sql(
                drop_constraint("hat_kill_switch", "ck_hat_kill_switch_scope"),
                drop_constraint("hat_kill_switch", "ck_hat_kill_switch_scope_bot"),
            ),
        ),
        (
            "bot execution_mode default dropped",
            sql("ALTER TABLE hat_bot ALTER COLUMN execution_mode DROP DEFAULT"),
        ),
    ],
    "governance.kill_switch_scope_check": [
        (
            "scope/bot CHECK dropped",
            sql(drop_constraint("hat_kill_switch", "ck_hat_kill_switch_scope_bot")),
        )
    ],
    "governance.exposure_checks": [
        (f"{name} dropped", sql(drop_constraint("hat_exposure_state", name)))
        for name in (
            "ck_hat_exposure_state_level",
            "ck_hat_exposure_state_wins",
            "ck_hat_exposure_state_losses",
        )
    ]
    + [("primary key (bot_id) dropped", drop_keyed("hat_exposure_state", "p", ("bot_id",)))],
    "portfolio.unique_keys": [
        (
            "unique (universe_id, version) dropped",
            drop_keyed("hat_universe_version", "u", ("universe_id", "version")),
        ),
        (
            "regime state primary key dropped",
            drop_keyed("hat_regime_state", "p", ("market_id", "model_version_id", "trade_date")),
        ),
        (
            "allocation primary key dropped",
            drop_keyed("hat_allocation_daily", "p", ("trade_date", "bot_id")),
        ),
    ],
    "portfolio.domain_checks": [
        (f"{name} dropped", sql(drop_constraint(table, name)))
        for table, name in (
            ("hat_universe", "ck_hat_universe_kind"),
            ("hat_regime_model_version", "ck_hat_regime_model_kind"),
            ("hat_regime_state", "ck_hat_regime_state_state"),
        )
    ],
    "ops.job_run_unique": [
        ("unique (job, trade_date) dropped", drop_keyed("hat_job_run", "u", ("job", "trade_date")))
    ],
    "ops.family_key_not_null": [
        (
            "family_key made nullable",
            sql("ALTER TABLE hat_backtest_run ALTER COLUMN family_key DROP NOT NULL"),
        )
    ],
    "governance.version_config_immutable": [
        (
            "trigger dropped",
            sql("DROP TRIGGER hat_strategy_version_immutable_guard ON hat_strategy_version"),
        ),
        (
            "trigger guards config but not config_hash",
            sql(
                """CREATE OR REPLACE FUNCTION hat_strategy_version_immutable() RETURNS trigger AS $$
                BEGIN
                  IF OLD.status <> 'draft' AND NEW.config IS DISTINCT FROM OLD.config THEN
                    RAISE EXCEPTION 'immutable' USING ERRCODE = 'check_violation';
                  END IF;
                  RETURN NEW;
                END $$ LANGUAGE plpgsql"""
            ),
        ),
        (
            "trigger lets a published version return to draft",
            sql(
                """CREATE OR REPLACE FUNCTION hat_strategy_version_immutable() RETURNS trigger AS $$
                BEGIN
                  IF OLD.status <> 'draft' AND NEW.status <> 'draft' AND (
                     NEW.config IS DISTINCT FROM OLD.config
                     OR NEW.config_hash IS DISTINCT FROM OLD.config_hash) THEN
                    RAISE EXCEPTION 'immutable' USING ERRCODE = 'check_violation';
                  END IF;
                  RETURN NEW;
                END $$ LANGUAGE plpgsql"""
            ),
        ),
    ],
    "governance.universe_version_immutable": [
        (
            "trigger dropped",
            sql("DROP TRIGGER hat_universe_version_immutable_guard ON hat_universe_version"),
        )
    ],
    "governance.one_live_version": [
        ("strategy partial unique index dropped", sql("DROP INDEX hat_one_live_version")),
        ("universe partial unique index dropped", sql("DROP INDEX hat_one_live_universe_version")),
    ],
    "governance.live_requires_evidence": [
        (
            "live-evidence CHECK dropped",
            sql(drop_constraint("hat_strategy_version", "ck_hat_strategy_version_live_evidence")),
        )
    ],
    "governance.risk_increase_cooling_off": [
        (
            "cooling-off CHECK dropped",
            sql(drop_constraint("hat_risk_policy_version", "hat_risk_increase_cooling_off")),
        ),
        (
            "cooling-off shortened to 6 days",
            sql(
                drop_constraint("hat_risk_policy_version", "hat_risk_increase_cooling_off"),
                "ALTER TABLE hat_risk_policy_version ADD CONSTRAINT hat_risk_increase_cooling_off"
                " CHECK (direction <> 'increase'"
                " OR effective_from >= requested_at + interval '144 hours')",
            ),
        ),
    ],
    "governance.risk_version_immutable": [
        (
            "trigger dropped",
            sql("DROP TRIGGER hat_risk_policy_version_immutable_guard ON hat_risk_policy_version"),
        ),
        (
            "trigger no longer blocks DELETE",
            sql(
                "DROP TRIGGER hat_risk_policy_version_immutable_guard ON hat_risk_policy_version",
                "CREATE TRIGGER hat_risk_policy_version_immutable_guard"
                " BEFORE UPDATE ON hat_risk_policy_version FOR EACH ROW"
                " EXECUTE FUNCTION hat_risk_policy_version_immutable()",
            ),
        ),
    ],
}

CASES = [(rule, label, apply) for rule, items in VARIANTS.items() for label, apply in items]


@pytest.mark.postgres
@pytest.mark.parametrize(("rule", "label", "apply"), CASES, ids=[f"{c[0]}:{c[1]}" for c in CASES])
def test_check_fails_when_rule_is_broken(
    conn: Connection, rule: str, label: str, apply: Break
) -> None:
    check = getattr(rules, "check_" + rule.replace(".", "__"))
    apply(conn)
    with pytest.raises(AssertionError):
        check(conn)
    _ = label


@pytest.mark.postgres
def test_migration_check_fails_when_ensure_is_not_idempotent(pg_engine: Engine) -> None:
    def broken_ensure(engine: Engine) -> None:
        with engine.begin() as connection:
            for statement in rules.broken_ddl_without_replace():
                connection.execute(text(statement))

    with pytest.raises(AssertionError):
        rules.check_migration__ensure_idempotent(pg_engine, broken_ensure)
    assert PG_DDL  # the real ensure is untouched by the variant


@pytest.mark.postgres
def test_every_check_has_a_broken_variant() -> None:
    names = {n for n in dir(rules) if n.startswith("check_") and "concurrent" not in n}
    expected = {"check_" + rule.replace(".", "__") for rule in VARIANTS}
    assert names - expected == {"check_migration__ensure_idempotent"}
    assert not any(isinstance(item, DBAPIError) for item in names)
