"""PBT telegram.retention_purge (HAT-TG-050): the scheduled purge job keeps exactly the last
year of Telegram messages and command-log rows."""

from datetime import UTC, date, datetime, timedelta

from hat_support import memory_engine
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.hat.models import HatCommandLog, HatJobRun, HatTelegramMessage
from app.hat.ops.jobs import JobResult, RunStatus, run_job
from app.hat.ops.retention import purge_job
from app.hat.time import IST, as_utc

ENGINE = memory_engine(HatTelegramMessage, HatCommandLog, HatJobRun)
FACTORY = sessionmaker(bind=ENGINE)
JOB = "retention.purge"

days = st.dates(min_value=date(2024, 1, 1), max_value=date(2040, 12, 31))
stamps = st.lists(
    st.tuples(st.integers(min_value=-5, max_value=900), st.integers(min_value=0, max_value=23)),
    max_size=12,
)


def instant(day: date, age: tuple[int, int]) -> datetime:
    days_ago, hour = age
    return datetime.combine(day - timedelta(days=days_ago), datetime.min.time(), tzinfo=UTC) + (
        timedelta(hours=hour)
    )


@given(day=days, messages=stamps, commands=stamps)
def test_retention_purge(
    day: date, messages: list[tuple[int, int]], commands: list[tuple[int, int]]
) -> None:
    """After the `retention.purge` job runs for a date d, each chat table holds exactly the rows
    stamped on or after d minus one year (IST date), the job is recorded as succeeded once, and
    running it again for the same date does not touch the tables."""
    with FACTORY() as session:
        session.query(HatTelegramMessage).delete()
        session.query(HatCommandLog).delete()
        session.query(HatJobRun).delete()
        for number, age in enumerate(messages):
            session.add(
                HatTelegramMessage(
                    chat_id=7, kind="card", sent_at=instant(day, age), message_id=number
                )
            )
        for age in commands:
            session.add(
                HatCommandLog(
                    chat_id=7,
                    command="/status",
                    accepted=True,
                    received_at=instant(day, age),
                )
            )
        session.commit()

    def body() -> JobResult:
        with FACTORY() as session:
            return purge_job(session, day)

    first = run_job(JOB, day, body, session_factory=FACTORY)
    assert first.status is RunStatus.SUCCEEDED and first.executed

    floor = date(day.year - 1, day.month, 28 if (day.month, day.day) == (2, 29) else day.day)
    with FACTORY() as session:
        kept_messages = sorted(
            as_utc(r.sent_at) for r in session.scalars(select(HatTelegramMessage))
        )
        kept_commands = sorted(
            as_utc(r.received_at) for r in session.scalars(select(HatCommandLog))
        )
    expected_messages = sorted(
        t for t in (instant(day, a) for a in messages) if t.astimezone(IST).date() >= floor
    )
    expected_commands = sorted(
        t for t in (instant(day, a) for a in commands) if t.astimezone(IST).date() >= floor
    )
    assert kept_messages == expected_messages
    assert kept_commands == expected_commands
    assert first.rows == len(messages) + len(commands) - len(expected_messages) - len(
        expected_commands
    )

    second = run_job(JOB, day, body, session_factory=FACTORY)
    assert not second.executed
    assert second.rows == first.rows
