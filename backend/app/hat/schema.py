"""Postgres-only DDL for HAT and the reference seed.

`ensure_hat_schema(bind)` runs from `init_db()` after `create_all`. It is idempotent
(`CREATE OR REPLACE`, `IF NOT EXISTS`, guarded constraints) and issues nothing on SQLite, where
the rules the app can enforce are enforced in the app. Every statement names only `hat_*` objects
(HAT-SCH-106); `PG_DDL` is the complete list so a check can read it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import time
from typing import TypedDict

from sqlalchemy import Connection, Engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..base import Base
from .confighash import canonical_json, config_hash
from .models import HatMarket
from .ops.partitions import ensure_event_partitions
from .time import utc_now

logger = logging.getLogger(__name__)

# One transaction-scoped lock key so two processes starting together do not interleave DDL.
_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('hat_ensure_schema'))"

_VERSION_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION hat_strategy_version_immutable() RETURNS trigger AS $$
BEGIN
  IF OLD.status <> 'draft' THEN
    IF NEW.config IS DISTINCT FROM OLD.config OR NEW.config_hash IS DISTINCT FROM OLD.config_hash
    THEN
      RAISE EXCEPTION 'strategy version % is % and its config cannot change', OLD.id, OLD.status
        USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status = 'draft' THEN
      RAISE EXCEPTION 'strategy version % cannot return to draft', OLD.id
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""

_UNIVERSE_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION hat_universe_version_immutable() RETURNS trigger AS $$
BEGIN
  IF OLD.status <> 'draft' THEN
    IF NEW.definition IS DISTINCT FROM OLD.definition
       OR NEW.config_hash IS DISTINCT FROM OLD.config_hash
    THEN
      RAISE EXCEPTION 'universe version % is % and its definition cannot change',
        OLD.id, OLD.status USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status = 'draft' THEN
      RAISE EXCEPTION 'universe version % cannot return to draft', OLD.id
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""

_RISK_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION hat_risk_policy_version_immutable() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'risk policy version % cannot be deleted', OLD.id
      USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.superseded_at IS NOT NULL
     OR (to_jsonb(NEW) - 'superseded_at') IS DISTINCT FROM (to_jsonb(OLD) - 'superseded_at')
  THEN
    RAISE EXCEPTION 'risk policy version % is immutable except setting superseded_at', OLD.id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""

# 7 days stated as 168 hours: a timestamptz + '7 days' follows the session time zone's
# daylight saving rules, a span in hours does not.
_COOLING_OFF_CHECK = """
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'hat_risk_increase_cooling_off'
      AND conrelid = to_regclass('hat_risk_policy_version')
  ) THEN
    ALTER TABLE hat_risk_policy_version ADD CONSTRAINT hat_risk_increase_cooling_off
      CHECK (direction <> 'increase' OR effective_from >= requested_at + interval '168 hours');
  END IF;
END $$
"""


# The lifecycle state domains (HAT-SCH-059) as the models declare them, for a table created before
# the models had them: `create_all` never alters an existing table. The text matches the models'
# CHECKs, so both paths give one catalog (DB `migration.ensure_idempotent` compares them).
_SIGNAL_STATE_CHECK = """
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'hat_signal_state_check' AND conrelid = to_regclass('hat_signal')
  ) THEN
    ALTER TABLE hat_signal ADD CONSTRAINT hat_signal_state_check CHECK (state IN (
      'Watchlisted', 'Triggered', 'AwaitingApproval', 'EntryPlaced', 'Open', 'PartiallyExited',
      'ExitPending', 'Closed', 'Rejected', 'Expired', 'Cancelled'));
  END IF;
END $$
"""

_POSITION_STATE_CHECK = """
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'hat_position_state_check' AND conrelid = to_regclass('hat_position')
  ) THEN
    ALTER TABLE hat_position ADD CONSTRAINT hat_position_state_check
      CHECK (state IN ('Open', 'PartiallyExited', 'ExitPending', 'Closed'));
  END IF;
END $$
"""

# The confidence domain (PR 71) for a `hat_signal` created before the column existed; the text
# matches the model's CHECK. Rows of that time hold no label (null), which reads as low.
_SIGNAL_CONFIDENCE_CHECK = """
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'hat_signal_confidence_check' AND conrelid = to_regclass('hat_signal')
  ) THEN
    ALTER TABLE hat_signal ADD CONSTRAINT hat_signal_confidence_check
      CHECK (confidence IN ('high', 'medium', 'low'));
  END IF;
END $$
"""

# A corporate-action factor is positive and a number (audit L2: Postgres orders numeric NaN above
# every finite value, so `> 0` alone accepts it); the text matches the model's CHECK.
_FACTOR_CHECK = """
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'hat_corporate_action_factor_check'
      AND conrelid = to_regclass('hat_corporate_action')
  ) THEN
    ALTER TABLE hat_corporate_action ADD CONSTRAINT hat_corporate_action_factor_check
      CHECK (adjustment_factor > 0 AND adjustment_factor <> 'NaN');
  END IF;
END $$
"""

# CA5: when a stored factor became knowable (`data/ca_known.py`); the text matches the model's.
_KNOWN_CHECK = """
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'hat_corporate_action_known_check'
      AND conrelid = to_regclass('hat_corporate_action')
  ) THEN
    ALTER TABLE hat_corporate_action ADD CONSTRAINT hat_corporate_action_known_check
      CHECK ((known_on IS NULL AND known_basis IS NULL) OR (known_on IS NOT NULL AND known_basis
        IS NOT NULL AND known_on >= ex_date AND known_basis IN ('ex_open', 'settled_close',
        'resulting_first_close', 'research_source_date', 'correction')));
  END IF;
END $$
"""

# A demerger is a stored fact, not a word in the text (`hat_corporate_action.demerger`). A
# database created before the column (hat_live) gains it once, with the rows the text named a
# demerger until then (an `other` whose text contains "demerger") marked; a database that has the
# column never runs this again, so no later text makes a row a demerger.
_DEMERGER_COLUMN = """
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_attribute
    WHERE attrelid = to_regclass('hat_corporate_action') AND attname = 'demerger'
      AND NOT attisdropped
  ) THEN
    ALTER TABLE hat_corporate_action ADD COLUMN demerger boolean NOT NULL DEFAULT false;
    UPDATE hat_corporate_action SET demerger = true
      WHERE action_type = 'other' AND raw_text ILIKE '%demerger%';
    ALTER TABLE hat_corporate_action ADD CONSTRAINT hat_corporate_action_demerger_check
      CHECK (NOT demerger OR action_type = 'other');
  END IF;
END $$
"""

# One instrument holds a symbol on any day (audit M3): no two rows of the symbol history with one
# symbol overlap (a null `valid_to` is open). Checked at commit, as a writer closes the previous
# holder's interval and adds the new one in either order. It needs `btree_gist`, and is added
# only once no overlap is stored (`admin repair-symbol-history --apply` closes them), so a
# startup never fails on it: `ensure_hat_schema` warns while it is missing.
_SYMBOL_HISTORY_EXCLUDE = """
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'hat_symbol_history_no_overlap'
      AND conrelid = to_regclass('hat_instrument_symbol_history')
  ) AND NOT EXISTS (
    SELECT 1 FROM hat_instrument_symbol_history a JOIN hat_instrument_symbol_history b
      ON a.symbol = b.symbol AND a.ctid < b.ctid
      AND daterange(a.valid_from, a.valid_to) && daterange(b.valid_from, b.valid_to)
  ) THEN
    BEGIN
      CREATE EXTENSION IF NOT EXISTS btree_gist;
    EXCEPTION WHEN insufficient_privilege OR undefined_file THEN
      RETURN;
    END;
    ALTER TABLE hat_instrument_symbol_history ADD CONSTRAINT hat_symbol_history_no_overlap
      EXCLUDE USING gist (symbol WITH =, daterange(valid_from, valid_to) WITH &&)
      DEFERRABLE INITIALLY DEFERRED;
  END IF;
END $$
"""

# A strategy version's release states are named apart from a sleeve's (`governance.VersionStatus`).
# A database created with the old names (hat_live) moves to the new ones once, in this
# transaction: its CHECKs and one-released index are dropped, the stored statuses renamed
# (backtested -> frozen, shadow -> trial, live -> released, retired -> superseded; the
# immutability trigger allows a status change that keeps the config), and the CHECKs and index
# made again with the models' text. A database that has the new index never runs it again.
_VERSION_STATUS_RENAME = """
DO $$
BEGIN
  IF to_regclass('hat_strategy_version') IS NOT NULL
     AND to_regclass('hat_one_released_version') IS NULL THEN
    ALTER TABLE hat_strategy_version DROP CONSTRAINT IF EXISTS ck_hat_strategy_version_status;
    ALTER TABLE hat_strategy_version
      DROP CONSTRAINT IF EXISTS ck_hat_strategy_version_live_evidence;
    DROP INDEX IF EXISTS hat_one_live_version;
    UPDATE hat_strategy_version SET status = CASE status
        WHEN 'backtested' THEN 'frozen' WHEN 'shadow' THEN 'trial'
        WHEN 'live' THEN 'released' WHEN 'retired' THEN 'superseded' ELSE status END
      WHERE status IN ('backtested', 'shadow', 'live', 'retired');
    ALTER TABLE hat_strategy_version ADD CONSTRAINT hat_strategy_version_status_check
      CHECK (status IN ('draft', 'frozen', 'trial', 'released', 'superseded'));
    ALTER TABLE hat_strategy_version ADD CONSTRAINT hat_strategy_version_release_evidence_check
      CHECK (status <> 'released'
        OR (backtest_run_id IS NOT NULL AND shadow_started_at IS NOT NULL));
    CREATE UNIQUE INDEX hat_one_released_version ON hat_strategy_version (strategy_id)
      WHERE status = 'released';
  END IF;
END $$
"""

# The close-phase exits once carried a `when` clock that nothing read (the window is the
# session's, `SessionCalendar.continuous_end_on`); the config model no longer has it, so a stored
# version holding it would not parse. Each such row loses the key once and its `config_hash` is
# recomputed (`_strip_exit_when`): the only config change a frozen version ever takes, made with
# its immutability trigger off inside this transaction, and only where the stored hash is the
# stored config's, so the new hash names exactly what the old one did, less the unread key.
_VERSION_GUARD = "hat_strategy_version_immutable_guard"
_EXITS_WITH_WHEN = """
SELECT id, config, config_hash FROM hat_strategy_version
WHERE jsonb_path_exists(config, '$.exits[*] ? (exists (@."when"))')
FOR UPDATE
"""


_SYMBOL_HISTORY_GUARDED = """
SELECT EXISTS (
  SELECT 1 FROM pg_constraint
  WHERE conname = 'hat_symbol_history_no_overlap'
    AND conrelid = to_regclass('hat_instrument_symbol_history')
)
"""

# A signal's confidence label and its basis are fixed when it is created (D132, HAT-EXEC-015).
_SIGNAL_CONFIDENCE_FN = """
CREATE OR REPLACE FUNCTION hat_signal_confidence_immutable() RETURNS trigger AS $$
BEGIN
  IF NEW.confidence IS DISTINCT FROM OLD.confidence
     OR NEW.confidence_basis IS DISTINCT FROM OLD.confidence_basis THEN
    RAISE EXCEPTION 'confidence of signal % is immutable', OLD.id;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""

# Invariant 1 (HAT-SCH-081, HAT-RISK-070): a long position's stop can only rise. The one exception
# is a split or bonus rescale (HAT-EXEC-141): the whole position moves to the new price scale, and
# only when a `position.rescaled` event of that position names exactly the old and new quantity,
# entry and stops. Such an event cannot authorise a second move: nothing but a rescale changes
# `initial_stop`, and a rescale only lowers it, so the position never holds its `from` again.
_STOP_ONLY_TIGHTENS_FN = """
CREATE OR REPLACE FUNCTION hat_stop_only_tightens() RETURNS trigger AS $$
BEGIN
  IF NEW.current_stop < OLD.current_stop AND NOT EXISTS (
    SELECT 1 FROM hat_event e
    WHERE e.aggregate_type = 'position' AND e.aggregate_id = OLD.id
      AND e.event_type = 'position.rescaled'
      AND (e.payload->'from'->>'qty_open')::bigint = OLD.qty_open
      AND (e.payload->'from'->>'avg_entry')::numeric = OLD.avg_entry
      AND (e.payload->'from'->>'initial_stop')::numeric = OLD.initial_stop
      AND (e.payload->'from'->>'current_stop')::numeric = OLD.current_stop
      AND (e.payload->'to'->>'qty_open')::bigint = NEW.qty_open
      AND (e.payload->'to'->>'avg_entry')::numeric = NEW.avg_entry
      AND (e.payload->'to'->>'initial_stop')::numeric = NEW.initial_stop
      AND (e.payload->'to'->>'current_stop')::numeric = NEW.current_stop
  ) THEN
    RAISE EXCEPTION 'stop for position % cannot move from % to %',
      OLD.id, OLD.current_stop, NEW.current_stop;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""

# A position is never deleted: a DELETE followed by an INSERT would restart `current_stop` below
# its old value and slip past the trigger above (invariant 1, HAT-SCH-081).
_POSITION_NO_DELETE_FN = """
CREATE OR REPLACE FUNCTION hat_position_never_deleted() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'position % cannot be deleted', OLD.id;
END $$ LANGUAGE plpgsql
"""

# The event log is append-only (HAT-SCH-061, HAT-DEC-I15).
_EVENT_APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION hat_event_is_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'hat_event is append-only';
END $$ LANGUAGE plpgsql
"""

# CA6: a corporate-action version is never changed or removed (`data/ca_versions.py`).
_ACTION_VERSION_APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION hat_corporate_action_version_is_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'hat_corporate_action_version is append-only';
END $$ LANGUAGE plpgsql
"""

PG_DDL: tuple[str, ...] = (
    _VERSION_IMMUTABLE_FN,
    """
CREATE OR REPLACE TRIGGER hat_strategy_version_immutable_guard
  BEFORE UPDATE ON hat_strategy_version
  FOR EACH ROW EXECUTE FUNCTION hat_strategy_version_immutable()
""",
    _UNIVERSE_IMMUTABLE_FN,
    """
CREATE OR REPLACE TRIGGER hat_universe_version_immutable_guard
  BEFORE UPDATE ON hat_universe_version
  FOR EACH ROW EXECUTE FUNCTION hat_universe_version_immutable()
""",
    _RISK_IMMUTABLE_FN,
    """
CREATE OR REPLACE TRIGGER hat_risk_policy_version_immutable_guard
  BEFORE UPDATE OR DELETE ON hat_risk_policy_version
  FOR EACH ROW EXECUTE FUNCTION hat_risk_policy_version_immutable()
""",
    _COOLING_OFF_CHECK,
    _SIGNAL_STATE_CHECK,
    _POSITION_STATE_CHECK,
    _SIGNAL_CONFIDENCE_CHECK,
    _FACTOR_CHECK,
    _SYMBOL_HISTORY_EXCLUDE,
    _VERSION_STATUS_RENAME,
    _SIGNAL_CONFIDENCE_FN,
    """
CREATE OR REPLACE TRIGGER hat_signal_confidence_guard
  BEFORE UPDATE OF confidence, confidence_basis ON hat_signal
  FOR EACH ROW EXECUTE FUNCTION hat_signal_confidence_immutable()
""",
    _STOP_ONLY_TIGHTENS_FN,
    """
CREATE OR REPLACE TRIGGER hat_position_stop_guard
  BEFORE UPDATE OF current_stop ON hat_position
  FOR EACH ROW EXECUTE FUNCTION hat_stop_only_tightens()
""",
    _POSITION_NO_DELETE_FN,
    """
CREATE OR REPLACE TRIGGER hat_position_no_delete_guard
  BEFORE DELETE ON hat_position
  FOR EACH ROW EXECUTE FUNCTION hat_position_never_deleted()
""",
    """
CREATE INDEX IF NOT EXISTS hat_announcement_instrument_published
  ON hat_announcement (instrument_id, published_at DESC)
""",
    # CA3a: a database created before the column (hat_live) gains it; create_all makes it anew
    "ALTER TABLE hat_corporate_action ADD COLUMN IF NOT EXISTS cash_amount numeric",
    # R-DIV: a trade's dividends; the rows before the column start at 0 and the nightly
    # `positions.restate_dividends` brings them up to the stored dividends
    "ALTER TABLE hat_trade_result ADD COLUMN IF NOT EXISTS dividends numeric(18,4)"
    " NOT NULL DEFAULT 0",
    # Kite's numeric instrument id of a zerodha row (None for the other brokers' rows)
    "ALTER TABLE hat_broker_symbol ADD COLUMN IF NOT EXISTS instrument_token bigint",
    _EVENT_APPEND_ONLY_FN,
    """
CREATE OR REPLACE TRIGGER hat_event_no_update
  BEFORE UPDATE OR DELETE ON hat_event
  FOR EACH ROW EXECUTE FUNCTION hat_event_is_append_only()
""",
    # C10: nor is it truncated. A statement trigger is not cloned to the partitions:
    # `ensure_event_partitions` puts the same one on each (`partition_guard_ddl`)
    """
CREATE OR REPLACE TRIGGER hat_event_no_truncate
  BEFORE TRUNCATE ON hat_event
  FOR EACH STATEMENT EXECUTE FUNCTION hat_event_is_append_only()
""",
    # DB7: indexes for the runtime's existing predicates (DB audit M7, L1)
    "CREATE INDEX IF NOT EXISTS hat_event_bot_type ON hat_event (bot_id, event_type)",
    "CREATE INDEX IF NOT EXISTS hat_order_signal ON hat_order (signal_id)",
    "CREATE INDEX IF NOT EXISTS hat_order_broker_account ON hat_order (broker_account_id)",
    "CREATE INDEX IF NOT EXISTS hat_position_signal ON hat_position (signal_id)",
    "CREATE INDEX IF NOT EXISTS hat_kill_switch_scope_bot ON hat_kill_switch (scope, bot_id)",
    "CREATE INDEX IF NOT EXISTS hat_mode_change_bot ON hat_mode_change (bot_id)",
    "CREATE INDEX IF NOT EXISTS hat_telegram_message_sent_at ON hat_telegram_message (sent_at)",
    "CREATE INDEX IF NOT EXISTS hat_command_log_received_at ON hat_command_log (received_at)",
    # CA5: a database created before the columns (hat_live) gains them
    "ALTER TABLE hat_corporate_action ADD COLUMN IF NOT EXISTS announced_on date",
    "ALTER TABLE hat_corporate_action ADD COLUMN IF NOT EXISTS known_on date",
    "ALTER TABLE hat_corporate_action ADD COLUMN IF NOT EXISTS known_basis text",
    _KNOWN_CHECK,
    _DEMERGER_COLUMN,
    "ALTER TABLE hat_isin_lineage ADD COLUMN IF NOT EXISTS known_on date",
    # Q1: drawdown points keep the allocation they were measured against
    "ALTER TABLE hat_daily_bot_snapshot ADD COLUMN IF NOT EXISTS capital numeric(18,4)",
    "ALTER TABLE hat_daily_bot_snapshot ADD COLUMN IF NOT EXISTS final_weight numeric",
    # CA6: create_all makes the table; its rows are append-only
    _ACTION_VERSION_APPEND_ONLY_FN,
    """
CREATE OR REPLACE TRIGGER hat_corporate_action_version_no_update
  BEFORE UPDATE OR DELETE ON hat_corporate_action_version
  FOR EACH ROW EXECUTE FUNCTION hat_corporate_action_version_is_append_only()
""",
)

NSE_MARKET = {
    "code": "NSE",
    "timezone": "Asia/Kolkata",
    "currency": "INR",
    "session_open": time(9, 15),
    "session_close": time(15, 30),
}


def ensure_hat_schema(bind: Engine | Connection) -> None:
    """Create or replace the Postgres-only triggers and CHECKs and create the `hat_event`
    partitions for the current month and the next two (HAT-SCH-066). No-op on other dialects."""
    if bind.dialect.name != "postgresql":
        return
    if isinstance(bind, Engine):
        with bind.begin() as connection:
            _run_ddl(connection)
    else:
        _run_ddl(bind)


def _run_ddl(connection: Connection) -> None:
    connection.execute(text(_LOCK_SQL))
    for statement in PG_DDL:
        connection.execute(text(statement))
    _strip_exit_when(connection)
    if not connection.execute(text(_SYMBOL_HISTORY_GUARDED)).scalar_one():
        logger.warning(
            "hat_symbol_history_no_overlap is missing: run `python -m app.hat.admin "
            "repair-symbol-history --apply` (and install btree_gist), then restart"
        )
    ensure_event_partitions(connection, utc_now().date())


def _strip_exit_when(connection: Connection) -> None:
    """Drop the unread `when` key from every stored version's exits and recompute its
    `config_hash` (`_EXITS_WITH_WHEN`). Idempotent: a row without the key is never read. Raises
    when a stored hash is not its stored config's, so no hash is ever made up."""
    rows = connection.execute(text(_EXITS_WITH_WHEN)).all()
    if not rows:
        return
    connection.execute(
        text(
            "ALTER TABLE hat_strategy_version DISABLE TRIGGER hat_strategy_version_immutable_guard"
        )
    )
    for row in rows:
        if config_hash(row.config) != row.config_hash:
            raise RuntimeError(f"strategy version {row.id}: stored config_hash is not its config's")
        config = dict(row.config)
        config["exits"] = [
            {k: v for k, v in rule.items() if k != "when"} for rule in config["exits"]
        ]
        connection.execute(
            text(
                "UPDATE hat_strategy_version SET config = CAST(:config AS jsonb),"
                " config_hash = :hash WHERE id = :id"
            ),
            {"config": canonical_json(config), "hash": config_hash(config), "id": row.id},
        )
    connection.execute(text(f"ALTER TABLE hat_strategy_version ENABLE TRIGGER {_VERSION_GUARD}"))
    logger.info("dropped the unread exit `when` from %d strategy versions", len(rows))


def seed_hat_reference(bind: Engine) -> None:
    """Insert the NSE `hat_market` row when missing (HAT-SCH-021). Safe to repeat and to race."""
    with Session(bind) as session:
        if session.scalar(select(HatMarket.id).where(HatMarket.code == "NSE")) is not None:
            return
        try:
            session.add(HatMarket(**NSE_MARKET))
            session.commit()
        except IntegrityError:
            session.rollback()  # another process seeded it first
            logger.debug("hat_market NSE already seeded")


# --- Schema drift (DB audit M5) ---------------------------------------------------------------
#
# `create_all` never alters an existing table, so a model change (a column, a CHECK, an index)
# that is merged but not hand-applied would fail only at its first write. At startup, after the
# upkeep above, `check_schema_drift` compares the live catalog with the catalog the models give:
# the models are created as temporary tables in the same transaction, both are read with the same
# catalog queries, and the transaction rolls back. Any difference refuses the start and names the
# DDL to run. Triggers, functions and partitions are `ensure_hat_schema`'s and are not compared.

# Tables the models do not hold that may stay until the owner drops them: the P6b ISIN-merge
# bookkeeping (runbook, DBA steps).
DRIFT_ALLOWED_TABLES = frozenset({"hat_isin_merge", "hat_isin_merge_row"})


class TableShape(TypedDict):
    """One table as the catalog describes it; definitions carry no schema qualifier."""

    columns: dict[str, tuple[str, bool]]  # name: (format_type, not null)
    constraints: dict[str, str]  # name: pg_get_constraintdef
    indexes: dict[str, str]  # name: pg_get_indexdef


SchemaShape = Mapping[str, TableShape]

_SHAPE_SQL = {
    "columns": """
SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull
FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
WHERE c.relnamespace = :ns AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND c.relname LIKE 'hat\\_%' AND a.attnum > 0 AND NOT a.attisdropped
""",
    # NOT NULL constraints (Postgres 18) are compared as the columns' nullability.
    "constraints": """
SELECT c.relname, k.conname, pg_get_constraintdef(k.oid)
FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
WHERE c.relnamespace = :ns AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND c.relname LIKE 'hat\\_%' AND k.contype <> 'n'
""",
    "indexes": """
SELECT c.relname, i.relname, pg_get_indexdef(i.oid)
FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class c ON c.oid = x.indrelid
WHERE c.relnamespace = :ns AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND c.relname LIKE 'hat\\_%'
""",
}

# The objects `PG_DDL` owns beyond the models: its indexes must exist (their definition is
# `PG_DDL`'s), and its constraints may (the symbol-history EXCLUDE waits for a repair).
_DDL_INDEX = re.compile(r"CREATE INDEX IF NOT EXISTS (\w+)\s+ON (\w+)")
_DDL_CONSTRAINT = re.compile(r"ALTER TABLE (\w+)\s+ADD CONSTRAINT (\w+)")


class SchemaDriftError(RuntimeError):
    """The database's schema differs from the models; the message holds the DDL to run."""


def pg_ddl_objects() -> tuple[dict[str, str], dict[str, str]]:
    """The indexes and constraints `PG_DDL` creates, each as {name: table}."""
    text_ = "\n".join(PG_DDL)
    indexes = dict(_DDL_INDEX.findall(text_))
    constraints = {name: table for table, name in _DDL_CONSTRAINT.findall(text_)}
    return indexes, constraints


def _read_shape(connection: Connection, namespace_sql: str) -> SchemaShape:
    namespace = connection.execute(text(f"SELECT {namespace_sql}")).scalar_one()
    schema = connection.execute(
        text("SELECT nspname FROM pg_namespace WHERE oid = :ns"), {"ns": namespace}
    ).scalar_one()
    qualified = re.compile(rf"\b(?:{re.escape(schema)}|pg_temp(?:_\d+)?)\.")
    shape: dict[str, TableShape] = {}

    def table(name: str) -> TableShape:
        return shape.setdefault(name, {"columns": {}, "constraints": {}, "indexes": {}})

    def unqualified(definition: str) -> str:
        # So the live (`public`) and the probe (`pg_temp`) texts compare.
        return qualified.sub("", definition).replace(" ON ONLY ", " ON ")

    params = {"ns": namespace}
    for name, column, type_, not_null in connection.execute(text(_SHAPE_SQL["columns"]), params):
        table(name)["columns"][column] = (type_, bool(not_null))
    for name, constraint, definition in connection.execute(text(_SHAPE_SQL["constraints"]), params):
        table(name)["constraints"][constraint] = unqualified(definition)
    for name, index, definition in connection.execute(text(_SHAPE_SQL["indexes"]), params):
        table(name)["indexes"][index] = unqualified(definition)
    return shape


def read_live_and_model_shapes(connection: Connection) -> tuple[SchemaShape, SchemaShape]:
    """The live schema and the models' schema, read on `connection` inside a transaction the
    caller rolls back (the models are created there as temporary tables)."""
    live = _read_shape(connection, "to_regnamespace(current_schema())::oid")
    probe = connection.execution_options(schema_translate_map={None: "pg_temp"})
    Base.metadata.create_all(probe, checkfirst=False)
    model = _read_shape(connection, "pg_my_temp_schema()")
    return live, model


def schema_drift(
    live: SchemaShape,
    model: SchemaShape,
    *,
    ddl_indexes: Mapping[str, str] | None = None,
    ddl_constraints: Mapping[str, str] | None = None,
    allowed_tables: frozenset[str] = DRIFT_ALLOWED_TABLES,
) -> list[str]:
    """Every difference between the live schema and the models', as DDL to run or (for what the
    database has and the models do not) a comment. Empty when they match."""
    ddl_indexes = ddl_indexes or {}
    ddl_constraints = ddl_constraints or {}
    drift: list[str] = []
    for table in sorted(model.keys() - live.keys()):
        drift.append(f"-- table {table} is missing: Base.metadata.create_all creates it")
    for table in sorted(live.keys() - model.keys() - allowed_tables):
        drift.append(f"-- table {table} is in the database, not in the models")
    for table in sorted(model.keys() & live.keys()):
        drift += _column_drift(table, live[table]["columns"], model[table]["columns"])
        table_ddl_indexes = {n for n, t in ddl_indexes.items() if t == table}
        table_ddl_constraints = {n for n, t in ddl_constraints.items() if t == table}
        drift += _named_drift(
            table,
            "constraint",
            live[table]["constraints"],
            model[table]["constraints"],
            table_ddl_constraints,
        )
        # A constraint's own index (the EXCLUDE's) carries the constraint's name.
        drift += _named_drift(
            table,
            "index",
            live[table]["indexes"],
            model[table]["indexes"],
            table_ddl_indexes | table_ddl_constraints,
        )
        for name in sorted(table_ddl_indexes):
            if name not in live[table]["indexes"] and name not in model[table]["indexes"]:
                drift.append(f"-- index {name} on {table} is missing (schema.PG_DDL creates it)")
    return drift


def _column_drift(
    table: str, live: Mapping[str, tuple[str, bool]], model: Mapping[str, tuple[str, bool]]
) -> list[str]:
    drift: list[str] = []
    for name in sorted(model.keys() - live.keys()):
        type_, not_null = model[name]
        if not not_null:
            drift.append(f"ALTER TABLE {table} ADD COLUMN {name} {type_};")
            continue
        # `ADD COLUMN ... NOT NULL` with no default fails on a populated table: add it nullable,
        # fill it, then set NOT NULL, in one transaction, so an unfilled table rolls it all back.
        drift.append(
            f"BEGIN;  -- {table}.{name} is NOT NULL: fill its existing rows, or this rolls back\n"
            f"ALTER TABLE {table} ADD COLUMN {name} {type_};\n"
            f"-- here: fill {name} on every existing row of {table}\n"
            f"ALTER TABLE {table} ALTER COLUMN {name} SET NOT NULL;\n"
            "COMMIT;"
        )
    for name in sorted(live.keys() - model.keys()):
        drift.append(f"-- column {table}.{name} is in the database, not in the models")
    for name in sorted(live.keys() & model.keys()):
        (live_type, live_not_null), (type_, not_null) = live[name], model[name]
        if live_type != type_:
            drift.append(
                f"ALTER TABLE {table} ALTER COLUMN {name} TYPE {type_};  -- is {live_type}"
            )
        if live_not_null != not_null:
            change = "SET" if not_null else "DROP"
            drift.append(f"ALTER TABLE {table} ALTER COLUMN {name} {change} NOT NULL;")
    return drift


def _named_drift(
    table: str,
    kind: str,
    live: Mapping[str, str],
    model: Mapping[str, str],
    tolerated: set[str],
) -> list[str]:
    def create(name: str) -> str:
        if kind == "index":
            return f"{model[name]};"
        return f"ALTER TABLE {table} ADD CONSTRAINT {name} {model[name]};"

    def drop(name: str) -> str:
        if kind == "index":
            return f"DROP INDEX {name};"
        return f"ALTER TABLE {table} DROP CONSTRAINT {name};"

    drift: list[str] = [create(name) for name in sorted(model.keys() - live.keys())]
    for name in sorted(live.keys() - model.keys() - tolerated):
        drift.append(f"-- {kind} {name} on {table} is in the database, not in the models")
    for name in sorted(live.keys() & model.keys()):
        if live[name] != model[name]:
            drift.append(f"{drop(name)}  -- is {live[name]}")
            drift.append(create(name))
    return drift


def check_schema_drift(bind: Engine) -> None:
    """Refuse a database whose schema differs from the models (DB audit M5): raise
    `SchemaDriftError` naming the DDL to run. No-op on other dialects."""
    if bind.dialect.name != "postgresql":
        return
    with bind.connect() as connection:
        transaction = connection.begin()
        try:
            live, model = read_live_and_model_shapes(connection)
        finally:
            transaction.rollback()
    ddl_indexes, ddl_constraints = pg_ddl_objects()
    drift = schema_drift(live, model, ddl_indexes=ddl_indexes, ddl_constraints=ddl_constraints)
    if drift:
        raise SchemaDriftError(
            "the database schema differs from the models; run this DDL, then restart:\n"
            + "\n".join(drift)
        )
