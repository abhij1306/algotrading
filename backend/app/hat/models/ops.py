"""Ops tables (03-schema §7): job runs and quality-gate results (PR 2); broker sessions,
Telegram messages, the command log and daily bot snapshots (PR 3); the Telegram cursors
(PR 16)."""

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
)

from ...base import Base
from ._types import Currency, JsonB, Money, Ratio, Timestamptz, fk, in_values, now_utc, uuid_pk

__all__ = [
    "JOB_STATUSES",
    "HatBrokerSession",
    "HatCommandLog",
    "HatDailyBotSnapshot",
    "HatJobRun",
    "HatQualityCheck",
    "HatTelegramCursor",
    "HatTelegramMessage",
]

JOB_STATUSES = ("running", "succeeded", "failed")


class HatJobRun(Base):
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


class HatQualityCheck(Base):
    """Gate results that block scans (02 §6)."""

    __tablename__ = "hat_quality_check"
    __table_args__ = (
        CheckConstraint(in_values("status", ("PASS", "FAIL")), name="ck_hat_quality_check_status"),
    )

    job_run_id = fk("hat_job_run.id", primary_key=True)
    check_name = Column(Text, primary_key=True)
    status = Column(Text, nullable=False)
    detail = Column(JsonB, nullable=False)


class HatBrokerSession(Base):
    """Morning login state."""

    __tablename__ = "hat_broker_session"

    broker_account_id = fk("hat_broker_account.id", primary_key=True)
    trade_date = Column(Date, primary_key=True)
    logged_in_at = Column(Timestamptz)
    expires_at = Column(Timestamptz)
    status = Column(Text, nullable=False)


class HatTelegramMessage(Base):
    """Links cards to signals. Purged after one year (HAT-SCH-091)."""

    __tablename__ = "hat_telegram_message"

    id = uuid_pk()
    chat_id = Column(BigInteger, nullable=False)
    kind = Column(Text, nullable=False)
    ref_id = Column(Uuid)
    sent_at = Column(Timestamptz, nullable=False, default=now_utc)
    message_id = Column(BigInteger, nullable=False)


class HatCommandLog(Base):
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


class HatTelegramCursor(Base):
    """Where the Telegram service has got to: `updates` is the next `getUpdates` offset
    (HAT-TG-011), `events` the last `hat_event` id the relay sent (HAT-TG-024). Not purged."""

    __tablename__ = "hat_telegram_cursor"

    name = Column(Text, primary_key=True)
    position = Column(BigInteger, nullable=False)
    updated_at = Column(Timestamptz, nullable=False, default=now_utc)


class HatDailyBotSnapshot(Base):
    """The kill-switch and drawdown source: one row per bot and day."""

    __tablename__ = "hat_daily_bot_snapshot"
    __table_args__ = (
        CheckConstraint(
            in_values("exposure_level", ("L1", "L2", "L3")),
            name="ck_hat_daily_bot_snapshot_exposure_level",
        ),
    )

    bot_id = fk("hat_bot.id", primary_key=True)
    trade_date = Column(Date, primary_key=True)
    equity = Column(Money, nullable=False)
    equity_peak = Column(Money, nullable=False)
    open_risk = Column(Money, nullable=False)
    realized_r = Column(Ratio, nullable=False)
    exposure_level = Column(Text, nullable=False)
    currency = Column(Currency, nullable=False)
