"""Ops tables. hat_live: the quality-gate results and the quality quarantine (the data
pipeline writes them). hat_state: job runs, broker sessions, Telegram messages and cursors, the
command log and the daily bot snapshots."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

from ...base import Base, StateBase
from ._types import Currency, JsonB, Money, Ratio, Timestamptz, fk, in_values, now_utc, ref, uuid_pk

__all__ = [
    "JOB_STATUSES",
    "HatBrokerSession",
    "HatCommandLog",
    "HatDailyBotSnapshot",
    "HatJobRun",
    "HatQualityCheck",
    "HatQualityQuarantine",
    "HatTelegramCursor",
    "HatTelegramMessage",
]

JOB_STATUSES = ("running", "succeeded", "failed")


class HatJobRun(StateBase):
    """One row per (job, trade_date): the unique key makes every job idempotent (T7)."""

    __tablename__ = "hat_job_run"
    __table_args__ = (
        UniqueConstraint("job", "trade_date"),
        CheckConstraint(in_values("status", JOB_STATUSES), name="ck_hat_job_run_status"),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="ck_hat_job_run_times"
        ),
    )

    id = uuid_pk()
    job = Column(Text, nullable=False)
    trade_date = Column(Date, nullable=False)
    started_at = Column(Timestamptz, nullable=False, default=now_utc)
    finished_at = Column(Timestamptz)
    status = Column(Text, nullable=False)
    calls = Column(Integer, nullable=False, default=0)
    rows = Column(Integer, nullable=False, default=0)
    # An error code, never raw provider or database text.
    error = Column(Text)
    # When the owner was told this run failed, missed or ran past its deadline (P5): once.
    alerted_at = Column(Timestamptz)


class HatQualityCheck(Base):
    """Gate results that block scans."""

    __tablename__ = "hat_quality_check"
    __table_args__ = (
        CheckConstraint(in_values("status", ("PASS", "FAIL")), name="ck_hat_quality_check_status"),
    )

    # the hat_state job run that checked (a plain column: the run is in the other database)
    job_run_id = ref(primary_key=True)
    check_name = Column(Text, primary_key=True)
    status = Column(Text, nullable=False)
    detail = Column(JsonB, nullable=False)


class HatQualityQuarantine(Base):
    """An unexplained big mover kept out of the scans from `since` until a corporate action with
    that ex-date is stored or the owner clears it (HAT-DATA-062)."""

    __tablename__ = "hat_quality_quarantine"
    __table_args__ = (
        CheckConstraint(
            in_values("released_by", ("corporate_action", "owner")),
            name="ck_hat_quality_quarantine_released_by",
        ),
    )

    instrument_id = fk("hat_instrument.id", primary_key=True)
    since = Column(Date, primary_key=True)
    quarantined_at = Column(Timestamptz, nullable=False, default=now_utc)
    released_at = Column(Timestamptz)
    released_by = Column(Text)


class HatBrokerSession(StateBase):
    """Morning login state."""

    __tablename__ = "hat_broker_session"

    broker_account_id = ref(primary_key=True)
    trade_date = Column(Date, primary_key=True)
    logged_in_at = Column(Timestamptz)
    expires_at = Column(Timestamptz)
    status = Column(Text, nullable=False)


class HatTelegramMessage(StateBase):
    """Links cards to signals. Purged after one year (HAT-SCH-091)."""

    __tablename__ = "hat_telegram_message"

    id = uuid_pk()
    chat_id = Column(BigInteger, nullable=False)
    kind = Column(Text, nullable=False)
    ref_id = Column(Uuid)
    sent_at = Column(Timestamptz, nullable=False, default=now_utc)
    message_id = Column(BigInteger, nullable=False)


class HatCommandLog(StateBase):
    """Every inbound command, including rejected ones from unknown chats. Purged after one
    year (HAT-SCH-091)."""

    __tablename__ = "hat_command_log"

    id = uuid_pk()
    chat_id = Column(BigInteger, nullable=False)
    command = Column(Text, nullable=False)
    args = Column(Text)
    accepted = Column(Boolean, nullable=False)
    reason = Column(Text)
    received_at = Column(Timestamptz, nullable=False, default=now_utc)


class HatTelegramCursor(StateBase):
    """Where the Telegram service has got to: `updates` is the next `getUpdates` offset
    (HAT-TG-011), `events` the last `hat_event` id the relay sent (HAT-TG-024). Not purged."""

    __tablename__ = "hat_telegram_cursor"

    name = Column(Text, primary_key=True)
    position = Column(BigInteger, nullable=False)
    updated_at = Column(Timestamptz, nullable=False, default=now_utc)


class HatDailyBotSnapshot(StateBase):
    """The kill-switch and drawdown source: one row per bot and day."""

    __tablename__ = "hat_daily_bot_snapshot"
    __table_args__ = (
        CheckConstraint(
            in_values("exposure_level", ("L1", "L2", "L3")),
            name="ck_hat_daily_bot_snapshot_exposure_level",
        ),
    )

    bot_id = ref(primary_key=True)
    trade_date = Column(Date, primary_key=True)
    # allocation capital + the realised P&L not yet in it + `open_pnl` (Q1; P5: marked to market
    # at the close)
    equity = Column(Money, nullable=False)
    equity_peak = Column(Money, nullable=False)
    # the marked P&L of the positions open at the snapshot (0 on rows before P5)
    open_pnl = Column(Money, nullable=False, default=0, server_default=text("0"))
    open_risk = Column(Money, nullable=False)
    realized_r = Column(Ratio, nullable=False)
    exposure_level = Column(Text, nullable=False)
    currency = Column(Currency, nullable=False)
    # the allocation the equity was measured against: its capital and final weight (None on
    # rows before Q1: unknown, so a change of allocation across them restates no peak)
    capital = Column(Money)
    final_weight = Column(Ratio)
