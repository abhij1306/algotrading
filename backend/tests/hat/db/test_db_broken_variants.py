"""Every DB check must fail when the rule it checks is removed (HAT-SCH-102, HAT-VER-032).

Each variant removes or weakens one trigger, index or constraint inside a transaction (Postgres
DDL is transactional, so nothing persists) and requires the matching check to raise
AssertionError. A check that still passes is vacuous.
"""

from collections.abc import Callable
from datetime import date, timedelta

import hat_db_rules as rules
import hat_db_runtime as runtime_rules
import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from app.hat.ops.partitions import months_to_cover, partition_bounds, partition_name
from app.hat.schema import PG_DDL

pytestmark = pytest.mark.postgres

Break = Callable[[Connection], None]
UNDO: list[Callable[[], None]] = []  # code-level breaks register how to put the code back


def _patch_trades(attribute: str, replacement: Callable[..., object]) -> None:
    from app.hat.engine import trades

    original = getattr(trades, attribute)
    setattr(trades, attribute, replacement)
    UNDO.append(lambda: setattr(trades, attribute, original))


def no_bot_lock(_conn: Connection) -> None:
    """The code, not the database, is broken: `lock_bot` does nothing."""
    _patch_trades("lock_bot", lambda *_args, **_kwargs: None)


def per_call_bot_lock(_conn: Connection) -> None:
    """The lock is keyed by something unique to each call, so two entries never share it."""
    import uuid

    from app.hat.engine import trades

    real = trades.lock_bot
    _patch_trades("lock_bot", lambda session, _bot_id: real(session, uuid.uuid4()))


def no_publish_lock(_conn: Connection) -> None:
    """The code is broken: the nightly publish takes no bot lock."""
    from app.hat.engine import watchlist

    original = watchlist.lock_bot
    watchlist.lock_bot = lambda *_args, **_kwargs: None
    UNDO.append(lambda: setattr(watchlist, "lock_bot", original))


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


def runtime_domain_variants() -> list[tuple[str, Break]]:
    return [
        (f"{name} dropped", sql(drop_constraint(table, name)))
        for table, name in (
            ("hat_order", "ck_hat_order_purpose"),
            ("hat_order", "ck_hat_order_side"),
            ("hat_order", "ck_hat_order_qty"),
            ("hat_approval", "ck_hat_approval_decision"),
            ("hat_signal", "ck_hat_signal_mode"),
            ("hat_signal", "ck_hat_signal_qty"),
            ("hat_fill", "ck_hat_fill_qty"),
            ("hat_position", "ck_hat_position_qty_open"),
            ("hat_fill", "ck_hat_fill_broker_trade_id"),
            ("hat_position", "ck_hat_position_stop_tightens"),
        )
    ]


def stop_function(condition: str) -> Break:
    """Replace the invariant-1 function with one that refuses on `condition`."""
    return sql(
        "CREATE OR REPLACE FUNCTION hat_stop_only_tightens() RETURNS trigger AS $$ BEGIN"
        f" IF {condition} THEN RAISE EXCEPTION 'stop cannot move'; END IF; RETURN NEW;"
        " END $$ LANGUAGE plpgsql"
    )


def event_function(body: str) -> Break:
    return sql(
        "CREATE OR REPLACE FUNCTION hat_event_is_append_only() RETURNS trigger AS $$ BEGIN"
        f" {body} END $$ LANGUAGE plpgsql"
    )


def plain_event_table() -> Break:
    """Replace the partitioned `hat_event` with an unpartitioned table of the same columns."""
    return sql(
        "CREATE TABLE hat_event_plain (LIKE hat_event INCLUDING ALL)",
        "DROP TABLE hat_event CASCADE",
        "ALTER TABLE hat_event_plain RENAME TO hat_event",
    )


VARIANTS_RUNTIME: dict[str, list[tuple[str, Break]]] = {
    "runtime.signal_unique_per_day": [
        (
            "unique (bot, playbook, instrument, trade_date) dropped",
            drop_keyed("hat_signal", "u", ("bot_id", "playbook", "instrument_id", "trade_date")),
        )
    ],
    "runtime.signal_versions_not_null": [
        (
            "strategy_version_id made nullable",
            sql("ALTER TABLE hat_signal ALTER COLUMN strategy_version_id DROP NOT NULL"),
        ),
        (
            "risk_policy_version_id made nullable",
            sql("ALTER TABLE hat_signal ALTER COLUMN risk_policy_version_id DROP NOT NULL"),
        ),
        (
            "strategy version foreign key dropped",
            sql(drop_constraint("hat_signal", "hat_signal_strategy_version_id_fkey")),
        ),
        (
            "risk policy version foreign key dropped",
            sql(drop_constraint("hat_signal", "hat_signal_risk_policy_version_id_fkey")),
        ),
    ],
    "runtime.domain_checks": [*runtime_domain_variants()],
    "runtime.order_status_check": [
        ("status CHECK dropped", sql(drop_constraint("hat_order", "ck_hat_order_status")))
    ],
    "runtime.order_tag_unique": [("unique (tag) dropped", drop_keyed("hat_order", "u", ("tag",)))],
    "runtime.fill_unique": [
        (
            "unique (order_id, broker_trade_id) dropped",
            drop_keyed("hat_fill", "u", ("order_id", "broker_trade_id")),
        )
    ],
    "runtime.trade_result_once": [
        (
            "primary key (position_id) dropped",
            drop_keyed("hat_trade_result", "p", ("position_id",)),
        )
    ],
    "ops.snapshot_unique": [
        (
            "primary key (bot_id, trade_date) dropped",
            drop_keyed("hat_daily_bot_snapshot", "p", ("bot_id", "trade_date")),
        ),
        (
            "exposure level CHECK dropped",
            sql(
                drop_constraint(
                    "hat_daily_bot_snapshot", "ck_hat_daily_bot_snapshot_exposure_level"
                )
            ),
        ),
    ],
    "governance.stop_only_rises": [
        ("trigger dropped", sql("DROP TRIGGER hat_position_stop_guard ON hat_position")),
        (
            "trigger fires only on other columns",
            sql(
                "DROP TRIGGER hat_position_stop_guard ON hat_position",
                "CREATE TRIGGER hat_position_stop_guard BEFORE UPDATE OF state ON hat_position"
                " FOR EACH ROW EXECUTE FUNCTION hat_stop_only_tightens()",
            ),
        ),
        ("equal stop refused (< became <=)", stop_function("NEW.current_stop <= OLD.current_stop")),
        (
            "a drop of up to 1 allowed",
            stop_function("NEW.current_stop < OLD.current_stop - 1"),
        ),
        ("function allows every move", stop_function("false")),
        ("delete guard dropped", sql("DROP TRIGGER hat_position_no_delete_guard ON hat_position")),
    ],
    "runtime.state_domain": [
        (
            "signal state CHECK dropped",
            sql(drop_constraint("hat_signal", "hat_signal_state_check")),
        ),
        (
            "position state CHECK dropped",
            sql(drop_constraint("hat_position", "hat_position_state_check")),
        ),
    ],
    "runtime.per_bot_serialised": [
        ("the bot lock is a no-op", no_bot_lock),
        ("the bot lock is keyed per call", per_call_bot_lock),
    ],
    "runtime.publish_serialised": [("the publish takes no bot lock", no_publish_lock)],
    "events.append_only": [
        ("trigger dropped", sql("DROP TRIGGER hat_event_no_update ON hat_event")),
        (
            "trigger no longer blocks DELETE",
            sql(
                "DROP TRIGGER hat_event_no_update ON hat_event",
                "CREATE TRIGGER hat_event_no_update BEFORE UPDATE ON hat_event"
                " FOR EACH ROW EXECUTE FUNCTION hat_event_is_append_only()",
            ),
        ),
        (
            "trigger no longer blocks UPDATE",
            sql(
                "DROP TRIGGER hat_event_no_update ON hat_event",
                "CREATE TRIGGER hat_event_no_update BEFORE DELETE ON hat_event"
                " FOR EACH ROW EXECUTE FUNCTION hat_event_is_append_only()",
            ),
        ),
        ("function does not raise", event_function("RETURN NEW;")),
        (
            "function raises a different message",
            event_function("RAISE EXCEPTION 'nope';"),
        ),
    ],
    "events.partitioned": [
        ("hat_event is a plain table", plain_event_table()),
        ("actor CHECK dropped", sql(drop_constraint("hat_event", "ck_hat_event_actor"))),
    ],
}
VARIANTS.update(VARIANTS_RUNTIME)

CASES = [(rule, label, apply) for rule, items in VARIANTS.items() for label, apply in items]


@pytest.mark.postgres
@pytest.mark.parametrize(("rule", "label", "apply"), CASES, ids=[f"{c[0]}:{c[1]}" for c in CASES])
def test_check_fails_when_rule_is_broken(
    conn: Connection, rule: str, label: str, apply: Break
) -> None:
    name = "check_" + rule.replace(".", "__")
    check = getattr(rules, name, None) or getattr(runtime_rules, name)
    try:
        apply(conn)
        with pytest.raises(AssertionError):
            check(conn)
    finally:
        while UNDO:
            UNDO.pop()()
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
    names = {
        n
        for module in (rules, runtime_rules)
        for n in dir(module)
        if n.startswith("check_") and "concurrent" not in n
    }
    expected = {"check_" + rule.replace(".", "__") for rule in VARIANTS}
    # The ensure-step checks take the step as an argument: their broken variants are the
    # `test_*_check_fails_*` tests below.
    assert names - expected == {
        "check_migration__ensure_idempotent",
        "check_events__partitions_ahead",
    }
    assert not any(isinstance(item, DBAPIError) for item in names)


def ensure_with(statements_for: Callable[[date], list[str]]) -> Callable[[Connection, date], None]:
    def ensure(conn: Connection, day: date) -> None:
        for statement in statements_for(day):
            conn.execute(text(statement))

    return ensure


def partition_statement(first: date, lower: str | None = None, upper: str | None = None) -> str:
    low, high = partition_bounds(first)
    return (
        f"CREATE TABLE IF NOT EXISTS {partition_name(first)} PARTITION OF hat_event"
        f" FOR VALUES FROM ('{lower or low.isoformat()}') TO ('{upper or high.isoformat()}')"
    )


PARTITION_ENSURE_VARIANTS: dict[str, Callable[[date], list[str]]] = {
    "only the current month": lambda day: [partition_statement(months_to_cover(day, 0)[0])],
    "only the next month, not the one after": lambda day: [
        partition_statement(first) for first in months_to_cover(day, 1)
    ],
    "the month ahead skipped": lambda day: [
        partition_statement(first) for index, first in enumerate(months_to_cover(day)) if index != 1
    ],
    "month boundaries in IST instead of UTC": lambda day: [
        partition_statement(
            first,
            lower=(partition_bounds(first)[0] - timedelta(hours=5, minutes=30)).isoformat(),
            upper=(partition_bounds(first)[1] - timedelta(hours=5, minutes=30)).isoformat(),
        )
        for first in months_to_cover(day)
    ],
}


@pytest.mark.postgres
@pytest.mark.parametrize("label", sorted(PARTITION_ENSURE_VARIANTS))
def test_partitions_check_fails_when_ensure_is_broken(conn: Connection, label: str) -> None:
    broken = ensure_with(PARTITION_ENSURE_VARIANTS[label])
    with pytest.raises(AssertionError):
        runtime_rules.check_events__partitions_ahead(conn, broken)


@pytest.mark.postgres
def test_concurrent_signal_check_fails_without_the_unique_key(pg_engine: Engine) -> None:
    """The race check commits for real, so its break is undone by hand in `finally`."""
    columns = "bot_id, playbook, instrument_id, trade_date"
    with pg_engine.begin() as connection:
        name = connection.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE conrelid = to_regclass('hat_signal')"
                " AND contype = 'u'"
            )
        ).scalar_one()
        connection.execute(text(drop_constraint("hat_signal", name)))
    try:
        with pytest.raises(AssertionError):
            runtime_rules.check_runtime__signal_unique_concurrent(pg_engine)
    finally:
        with pg_engine.begin() as connection:
            connection.execute(
                text(f"ALTER TABLE hat_signal ADD CONSTRAINT {name} UNIQUE ({columns})")
            )
