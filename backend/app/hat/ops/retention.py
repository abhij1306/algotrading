"""Retention (03-schema §9, HAT-SCH-091, HAT-TG-050).

Telegram messages and the command log are kept one year. Events, signals, orders, positions and
results are kept forever, and so are Parquet bars, features and raw bhavcopy zips: this module
deletes only from the two chat tables.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..models import HatCommandLog, HatTelegramMessage
from ..time import IST
from .jobs import JobResult

__all__ = ["PURGED_TABLES", "RETENTION_YEARS", "purge_cutoff", "purge_chat_logs"]

RETENTION_YEARS = 1
PURGED_TABLES = ("hat_telegram_message", "hat_command_log")


def purge_cutoff(on: date) -> datetime:
    """The instant one year before the start of `on` (IST). A row stamped at or after it stays.

    29 February one year back is 28 February.
    """
    year = on.year - RETENTION_YEARS
    day = 28 if (on.month, on.day) == (2, 29) else on.day
    return datetime(year, on.month, day, tzinfo=IST).astimezone(UTC)


def purge_chat_logs(session: Session, on: date) -> int:
    """Delete messages sent and commands received before `purge_cutoff(on)`; commit nothing.
    Returns the number of rows deleted. Re-running it deletes nothing more."""
    cutoff = purge_cutoff(on)
    removed = session.execute(delete(HatTelegramMessage).where(HatTelegramMessage.sent_at < cutoff))
    removed_log = session.execute(delete(HatCommandLog).where(HatCommandLog.received_at < cutoff))
    return (removed.rowcount or 0) + (removed_log.rowcount or 0)


def purge_job(session: Session, on: date) -> JobResult:
    """Job body for `retention.purge`: purge, commit, report the rows deleted. Idempotent."""
    rows = purge_chat_logs(session, on)
    session.commit()
    return JobResult(rows=rows)
