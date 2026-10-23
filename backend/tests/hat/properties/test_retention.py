"""PBT retention.purge_scope: the purge deletes Telegram messages and command-log
rows older than a year and nothing else."""

import tempfile
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from hat_support import event_engine
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.hat.models import (
    HatCommandLog,
    HatEvent,
    HatJobRun,
    HatTelegramMessage,
)
from app.hat.ops.retention import PURGED_TABLES, purge_chat_logs, purge_job
from app.hat.time import IST, as_utc

ENGINE = event_engine(HatTelegramMessage, HatCommandLog, HatJobRun)

run_dates = st.dates(min_value=date(2024, 1, 1), max_value=date(2040, 12, 31))
# Row ages in days before the run date, around the one-year line (365 or 366 days).
ages = st.lists(
    st.tuples(st.integers(min_value=-3, max_value=800), st.integers(min_value=0, max_value=23)),
    max_size=10,
)


def year_before(day: date) -> date:
    return date(day.year - 1, day.month, 28 if (day.month, day.day) == (2, 29) else day.day)


def at(day: date, days_ago: int, hour: int) -> datetime:
    """The UTC instant `days_ago` days before `day`, at `hour`:00 UTC."""
    return datetime.combine(day - timedelta(days=days_ago), datetime.min.time(), tzinfo=UTC) + (
        timedelta(hours=hour)
    )


def on_or_after(instant: datetime, cutoff_day: date) -> bool:
    """Whether the instant's IST calendar date is on or after `cutoff_day`."""
    return instant.astimezone(IST).date() >= cutoff_day


def counts(session: Session) -> dict[str, int]:
    return {
        table.__tablename__: session.scalar(select(func.count()).select_from(table))
        for table in (HatTelegramMessage, HatCommandLog, HatJobRun, HatEvent)
    }


def seed(
    session: Session,
    day: date,
    message_ages: list[tuple[int, int]],
    command_ages: list[tuple[int, int]],
) -> None:
    for number, (age, hour) in enumerate(message_ages):
        session.add(
            HatTelegramMessage(
                chat_id=1, kind="card", sent_at=at(day, age, hour), message_id=number
            )
        )
    for number, (age, hour) in enumerate(command_ages):
        session.add(
            HatCommandLog(
                chat_id=1,
                command="/status",
                accepted=bool(number % 2),
                received_at=at(day, age, hour),
            )
        )
    # Rows of other tables, decades old: a purge of the chat tables must leave them alone.
    for number in range(3):
        session.add(
            HatJobRun(
                job=f"j{uuid.uuid4().hex}", trade_date=date(2011, 1, 1 + number), status="succeeded"
            )
        )
        session.add(
            HatEvent(
                id=10**9 + uuid.uuid4().int % 10**18,
                occurred_at=datetime(2011, 1, 1, tzinfo=UTC),
                recorded_at=datetime(2011, 1, 1 + number, tzinfo=UTC),
                user_id=uuid.uuid4(),
                aggregate_type="bot",
                aggregate_id=uuid.uuid4(),
                event_type="bot.paused",
                actor="engine",
                payload={},
            )
        )
    session.commit()


@given(day=run_dates, message_ages=ages, command_ages=ages)
def test_purge_scope(
    day: date, message_ages: list[tuple[int, int]], command_ages: list[tuple[int, int]]
) -> None:
    """For every set of rows and every run date, a chat row is gone exactly when it was stamped
    before the start (IST) of the same day one year earlier; every other table, and every file under
    the data root, is unchanged; a second run deletes nothing."""
    cutoff_day = year_before(day)
    with Session(ENGINE) as session:
        session.query(HatTelegramMessage).delete()
        session.query(HatCommandLog).delete()
        session.commit()
        seed(session, day, message_ages, command_ages)
        before = counts(session)
    with tempfile.TemporaryDirectory() as root:
        bars = Path(root) / "bars_daily_raw" / "date=2010-01-04"
        bars.mkdir(parents=True)
        (bars / "part.parquet").write_bytes(b"x")
        raw = Path(root) / "raw" / "bhav.zip"
        raw.parent.mkdir()
        raw.write_bytes(b"zip")
        with Session(ENGINE) as session:
            deleted = purge_chat_logs(session, day)
            session.commit()
        assert sorted(p.name for p in Path(root).rglob("*") if p.is_file()) == [
            "bhav.zip",
            "part.parquet",
        ]
    with Session(ENGINE) as session:
        after = counts(session)
        kept_messages = [
            as_utc(row.sent_at) for row in session.scalars(select(HatTelegramMessage)).all()
        ]
        kept_commands = [
            as_utc(row.received_at) for row in session.scalars(select(HatCommandLog)).all()
        ]
        again = purge_chat_logs(session, day)
    expected_messages = [
        t for t in (at(day, *a) for a in message_ages) if on_or_after(t, cutoff_day)
    ]
    expected_commands = [
        t for t in (at(day, *a) for a in command_ages) if on_or_after(t, cutoff_day)
    ]
    assert sorted(kept_messages) == sorted(expected_messages)
    assert sorted(kept_commands) == sorted(expected_commands)
    assert deleted == before["hat_telegram_message"] - len(kept_messages) + (
        before["hat_command_log"] - len(kept_commands)
    )
    for table, count in before.items():
        if table not in PURGED_TABLES:
            assert after[table] == count, table
    assert again == 0


@given(day=run_dates, message_ages=ages)
def test_purge_job_reports_and_is_idempotent(
    day: date, message_ages: list[tuple[int, int]]
) -> None:
    """The job body commits, reports the rows it deleted, and running it again changes nothing."""
    with Session(ENGINE) as session:
        session.query(HatTelegramMessage).delete()
        session.query(HatCommandLog).delete()
        session.commit()
        seed(session, day, message_ages, [])
        total = session.scalar(select(func.count()).select_from(HatTelegramMessage))
        first = purge_job(session, day)
        left = session.scalar(select(func.count()).select_from(HatTelegramMessage))
        second = purge_job(session, day)
    assert first.rows == total - left
    assert second.rows == 0
