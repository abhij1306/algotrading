"""Strategy, risk policy and bot tables.

A strategy holds versions; a bot deploys one live version to one broker account; the risk
policy is a separate versioned record that holds every risk number. A bot has no risk columns.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapper

from ...base import Base
from ..risk.modes import Mode
from ..risk.policy import require_cooling_off
from ._types import (
    VERSION_STATUSES,
    Currency,
    JsonB,
    Money,
    Timestamptz,
    fk,
    in_values,
    now_utc,
    uuid_pk,
)

BOT_ID = "hat_bot.id"
# `hat_broker_account.status` and `.broker` values: the one copy every reader imports, from this
# leaf so the API reads them without the execution or mode code (PR 73, PR 38 follow-up 3).
ACCOUNT_PENDING = "pending"
ACCOUNT_ACTIVE = "active"  # passed its capability check (HAT-BRK-092)
ACCOUNT_FAILED = "failed"
BROKER_FYERS = "fyers"

__all__ = [
    "ACCOUNT_ACTIVE",
    "ACCOUNT_FAILED",
    "ACCOUNT_PENDING",
    "BROKER_FYERS",
    "HatBot",
    "HatBrokerAccount",
    "HatCriterionType",
    "HatExposureState",
    "HatKillSwitch",
    "HatModeChange",
    "HatRiskPolicyVersion",
    "HatStrategy",
    "HatStrategyVersion",
]


class HatCriterionType(Base):
    __tablename__ = "hat_criterion_type"
    __table_args__ = (
        CheckConstraint(
            in_values("stage", ("universe", "setup", "trigger", "entry_filter")),
            name="ck_hat_criterion_type_stage",
        ),
    )

    code = Column(Text, primary_key=True)
    version = Column(Integer, primary_key=True)
    stage = Column(Text, nullable=False)
    param_schema = Column(JsonB, nullable=False)
    data_needs = Column(JsonB, nullable=False)


class HatStrategy(Base):
    __tablename__ = "hat_strategy"

    id = uuid_pk()
    user_id = Column(Uuid, nullable=False)
    market_id = fk("hat_market.id")
    name = Column(Text, nullable=False)
    persona = Column(Text, nullable=False)
    created_at = Column(Timestamptz, nullable=False, default=now_utc)


class HatStrategyVersion(Base):
    __tablename__ = "hat_strategy_version"
    __table_args__ = (
        UniqueConstraint("strategy_id", "version"),
        CheckConstraint(
            in_values("status", VERSION_STATUSES), name="ck_hat_strategy_version_status"
        ),
        CheckConstraint(
            "status <> 'live' OR (backtest_run_id IS NOT NULL AND shadow_started_at IS NOT NULL)",
            name="ck_hat_strategy_version_live_evidence",
        ),
        # hat_backtest_run references this table back, so this side is added with ALTER.
        ForeignKeyConstraint(
            ["backtest_run_id"],
            ["hat_backtest_run.id"],
            name="fk_hat_strategy_version_backtest_run",
            use_alter=True,
        ),
        Index(
            "hat_one_live_version",
            "strategy_id",
            unique=True,
            postgresql_where=text("status = 'live'"),
            sqlite_where=text("status = 'live'"),
        ),
    )

    id = uuid_pk()
    strategy_id = fk("hat_strategy.id")
    version = Column(Integer, nullable=False)
    status = Column(Text, nullable=False)
    config = Column(JsonB, nullable=False)
    config_hash = Column(Text, nullable=False)
    parent_version_id = Column(Uuid, ForeignKey("hat_strategy_version.id"))
    backtest_run_id = Column(Uuid)
    shadow_started_at = Column(Timestamptz)
    promoted_at = Column(Timestamptz)
    created_at = Column(Timestamptz, nullable=False, default=now_utc)


class HatRiskPolicyVersion(Base):
    __tablename__ = "hat_risk_policy_version"
    __table_args__ = (
        UniqueConstraint("user_id", "version"),
        CheckConstraint(
            in_values("direction", ("initial", "increase", "decrease")),
            name="ck_hat_risk_policy_direction",
        ),
        # The 7-day cooling-off CHECK needs an interval, so `ensure_hat_schema` adds it on
        # Postgres; `_enforce_cooling_off` below enforces the same rule in the app.
    )

    id = uuid_pk()
    user_id = Column(Uuid, nullable=False)
    version = Column(Integer, nullable=False)
    policy = Column(JsonB, nullable=False)
    direction = Column(Text, nullable=False)
    requested_at = Column(Timestamptz, nullable=False, default=now_utc)
    effective_from = Column(Timestamptz, nullable=False)
    gate_evidence = Column(JsonB)
    superseded_at = Column(Timestamptz)


@event.listens_for(HatRiskPolicyVersion, "before_insert")
@event.listens_for(HatRiskPolicyVersion, "before_update")
def _enforce_cooling_off(
    _mapper: Mapper[HatRiskPolicyVersion],
    _connection: Connection,
    target: HatRiskPolicyVersion,
) -> None:
    """HAT-SCH-084 in the app, so SQLite (tests) enforces what the Postgres CHECK does."""
    if target.requested_at is None:
        # Fill the default now, so the instant checked here is the one stored.
        target.requested_at = now_utc()
    require_cooling_off(target.direction, target.requested_at, target.effective_from)


class HatBrokerAccount(Base):
    __tablename__ = "hat_broker_account"

    id = uuid_pk()
    user_id = Column(Uuid, nullable=False)
    broker = Column(Text, nullable=False)
    account_ref = Column(Text, nullable=False)
    capabilities = Column(JsonB, nullable=False)
    status = Column(Text, nullable=False)
    # The owner's word that DDPI (or a POA) is registered on the demat, so a CNC sale goes
    # through without a TPIN prompt. Set only through the admin command
    # line, with the time and a dated note; until then no bot on the account runs confirm or
    # auto (HAT-GOV-085).
    ddpi_confirmed = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    ddpi_confirmed_at = Column(Timestamptz)
    ddpi_note = Column(Text)


class HatBot(Base):
    """The sleeve. It holds no risk columns: every risk number lives in the risk policy."""

    __tablename__ = "hat_bot"
    __table_args__ = (
        CheckConstraint(
            in_values("execution_mode", Mode),
            name="ck_hat_bot_execution_mode",
        ),
        CheckConstraint(
            in_values("status", ("active", "paused", "manage_only")), name="ck_hat_bot_status"
        ),
        CheckConstraint(
            in_values(
                "lifecycle_state",
                ("research", "backtested", "shadow", "live", "probation", "retired"),
            ),
            name="ck_hat_bot_lifecycle_state",
        ),
    )

    id = uuid_pk()
    user_id = Column(Uuid, nullable=False)
    strategy_id = fk("hat_strategy.id")
    live_version_id = fk("hat_strategy_version.id", nullable=True)
    broker_account_id = fk("hat_broker_account.id")
    # Confirm by default (HAT-DEC-B2, D130/D132): the mode the owner wants. It routes shadow
    # until the sleeve is Live and the account and policy gates pass (`execution.modes`).
    execution_mode = Column(
        Text, nullable=False, default=Mode.CONFIRM, server_default=Mode.CONFIRM.value
    )
    status = Column(Text, nullable=False)
    universe_version_id = fk("hat_universe_version.id", nullable=True)
    lifecycle_state = Column(Text, nullable=False)
    risk_cluster = Column(Text, nullable=False)


class HatModeChange(Base):
    __tablename__ = "hat_mode_change"
    __table_args__ = (
        CheckConstraint(in_values("to_mode", Mode), name="ck_hat_mode_change_to_mode"),
    )

    id = uuid_pk()
    bot_id = fk(BOT_ID)
    from_mode = Column(Text, nullable=False)
    to_mode = Column(Text, nullable=False)
    requested_at = Column(Timestamptz, nullable=False, default=now_utc)
    # The next session open, from hat_trading_calendar (set by the app).
    effective_from = Column(Timestamptz, nullable=False)
    via = Column(Text, nullable=False)
    # What the session open did with it (`mode_changes.apply_mode_changes`): applied, or
    # refused with the gate that failed. Both stay null until its open.
    applied_at = Column(Timestamptz)
    refused_reason = Column(Text)


class HatKillSwitch(Base):
    __tablename__ = "hat_kill_switch"
    __table_args__ = (
        CheckConstraint(in_values("scope", ("account", "bot")), name="ck_hat_kill_switch_scope"),
        CheckConstraint(in_values("state", ("tripped", "armed")), name="ck_hat_kill_switch_state"),
        CheckConstraint(
            "(scope = 'bot' AND bot_id IS NOT NULL) OR (scope = 'account' AND bot_id IS NULL)",
            name="ck_hat_kill_switch_scope_bot",
        ),
    )

    id = uuid_pk()
    scope = Column(Text, nullable=False)
    bot_id = fk(BOT_ID, nullable=True)
    state = Column(Text, nullable=False)
    tripped_at = Column(Timestamptz, nullable=False)
    reason = Column(Text, nullable=False)
    rearmed_at = Column(Timestamptz)


class HatDrawdownBase(Base):
    """D232 (Q41): where a re-armed drawdown kill measures from. One row per re-arm row of a
    drawdown or manual kill, written at the first equity point after the re-arm: that point's
    time, its equity, the allocation capital it was measured against and the realised P&L of
    the scope's real trades closed by then (so later points need only the trades closed after
    it)."""

    __tablename__ = "hat_drawdown_base"

    rearm_id = fk("hat_kill_switch.id", primary_key=True)
    taken_at = Column(Timestamptz, nullable=False)
    equity = Column(Money, nullable=False)
    capital = Column(Money, nullable=False)
    realized = Column(Money, nullable=False)
    currency = Column(Currency, nullable=False)  # the scope's allocation currency


class HatExposureState(Base):
    __tablename__ = "hat_exposure_state"
    __table_args__ = (
        CheckConstraint(in_values("level", ("L1", "L2", "L3")), name="ck_hat_exposure_state_level"),
        CheckConstraint("consecutive_wins >= 0", name="ck_hat_exposure_state_wins"),
        CheckConstraint("consecutive_losses >= 0", name="ck_hat_exposure_state_losses"),
    )

    bot_id = fk(BOT_ID, primary_key=True)
    level = Column(Text, nullable=False)
    consecutive_wins = Column(Integer, nullable=False)
    consecutive_losses = Column(Integer, nullable=False)
    updated_at = Column(Timestamptz, nullable=False, default=now_utc)
