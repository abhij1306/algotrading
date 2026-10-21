"""Postgres-only DDL for HAT and the reference seed (03-schema §8, §10).

`ensure_hat_schema(bind)` runs from `init_db()` after `create_all`. It is idempotent
(`CREATE OR REPLACE`, `IF NOT EXISTS`, guarded constraints) and issues nothing on SQLite, where
the rules the app can enforce are enforced in the app. Every statement names only `hat_*` objects
(HAT-SCH-106); `PG_DDL` is the complete list so a check can read it.
"""

from __future__ import annotations

import logging
from datetime import time

from sqlalchemy import Connection, Engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateColumn

from ..base import Base
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

# Invariant 1 (HAT-SCH-081, HAT-RISK-070): a long position's stop can only rise.
_STOP_ONLY_TIGHTENS_FN = """
CREATE OR REPLACE FUNCTION hat_stop_only_tightens() RETURNS trigger AS $$
BEGIN
  IF NEW.current_stop < OLD.current_stop THEN
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
    _EVENT_APPEND_ONLY_FN,
    """
CREATE OR REPLACE TRIGGER hat_event_no_update
  BEFORE UPDATE OR DELETE ON hat_event
  FOR EACH ROW EXECUTE FUNCTION hat_event_is_append_only()
""",
)

NSE_MARKET = {
    "code": "NSE",
    "timezone": "Asia/Kolkata",
    "currency": "INR",
    "session_open": time(9, 15),
    "session_close": time(15, 30),
}


def ensure_hat_columns(bind: Engine) -> None:
    """Add columns the models gained since a database was created (HAT-SCH-103). No-op on
    other dialects. Idempotent and loss-free: it only runs `ADD COLUMN IF NOT EXISTS`.

    A new NOT NULL column without a server default is added nullable (existing rows have no
    value to give it); a later release tightens it once it is filled. Constraints and indexes
    on new columns are added explicitly in `PG_DDL`.
    """
    if bind.dialect.name != "postgresql":
        return
    with bind.begin() as connection:
        connection.execute(text(_LOCK_SQL))
        inspector = inspect(connection)
        present = set(inspector.get_table_names())
        for table in Base.metadata.sorted_tables:
            if not table.name.startswith("hat_") or table.name not in present:
                continue
            have = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                ddl = str(CreateColumn(column).compile(dialect=bind.dialect)).strip()
                if not column.nullable and column.server_default is None:
                    ddl = ddl.replace(" NOT NULL", "")
                connection.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN IF NOT EXISTS {ddl}')
                )
                logger.warning("Added missing column %s.%s", table.name, column.name)


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
    ensure_event_partitions(connection, utc_now().date())


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
