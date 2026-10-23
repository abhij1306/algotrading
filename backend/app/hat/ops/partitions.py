"""Monthly partitions of `hat_event` (HAT-SCH-066, job `events.partitions`).

`hat_event` is range-partitioned on `recorded_at`, which is UTC. A partition holds one UTC
month: [first day 00:00 UTC, first day of the next month 00:00 UTC). An insert with no matching
partition fails, so partitions must exist ahead of time: `ensure_hat_schema` creates the current
month and the next two, and the nightly `events.partitions` job keeps creating them ahead. The
job reads back which partitions exist: with less than one month ahead (or when the creation
fails) it alerts `events.partitions_short` and fails, so the shortage is seen weeks before an
insert would fail (PR 17, audit money-safety-engine-ops F11).

Every statement here is idempotent (`CREATE TABLE IF NOT EXISTS`) and names only `hat_*` objects.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import SQLAlchemyError

from ..notify import LogNotifier, Notifier
from ..time import as_utc
from .jobs import JobError, JobResult

logger = logging.getLogger(__name__)

PARENT_TABLE = "hat_event"
# The current month and this many following months always have a partition.
MONTHS_AHEAD = 2
# Fewer whole months than this ahead of the current one is alerted (and fails the job).
MIN_MONTHS_AHEAD = 1
CODE_PARTITIONS_SHORT = "events.partitions_short"


def month_start(day: date) -> date:
    """The first day of the month of `day`."""
    return day.replace(day=1)


def next_month(first: date) -> date:
    """The first day of the month after the one starting at `first`."""
    return date(first.year + (first.month == 12), first.month % 12 + 1, 1)


def month_of(instant: datetime) -> date:
    """The first day of the UTC month `instant` falls in: the partition it is routed to."""
    return month_start(as_utc(instant).date())


def partition_name(first: date) -> str:
    return f"{PARENT_TABLE}_p{first.year:04d}{first.month:02d}"


def months_to_cover(day: date, ahead: int = MONTHS_AHEAD) -> list[date]:
    """First days of the month of `day` and the `ahead` months after it."""
    months = [month_start(day)]
    for _ in range(ahead):
        months.append(next_month(months[-1]))
    return months


def partition_bounds(first: date) -> tuple[datetime, datetime]:
    """The half-open UTC range [from, to) of the partition starting on `first`."""
    return (
        datetime(first.year, first.month, 1, tzinfo=UTC),
        datetime.combine(next_month(first), datetime.min.time(), tzinfo=UTC),
    )


def partition_ddl(first: date) -> str:
    lower, upper = partition_bounds(first)
    # The bounds are formatted from dates, never from input text.
    return (
        f"CREATE TABLE IF NOT EXISTS {partition_name(first)} PARTITION OF {PARENT_TABLE} "
        f"FOR VALUES FROM ('{lower.isoformat()}') TO ('{upper.isoformat()}')"
    )


def ensure_event_partitions(
    connection: Connection, day: date, ahead: int = MONTHS_AHEAD
) -> list[str]:
    """Create the partitions covering `day`'s month and `ahead` months after it. Returns the
    names that now exist for that range. No-op (empty list) off Postgres."""
    if connection.dialect.name != "postgresql":
        return []
    names = []
    for first in months_to_cover(day, ahead):
        connection.execute(text(partition_ddl(first)))
        names.append(partition_name(first))
    return names


def existing_partitions(connection: Connection) -> set[str]:
    """The names of the partitions attached to `hat_event`."""
    rows = connection.execute(
        text(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = :parent"
        ),
        {"parent": PARENT_TABLE},
    )
    return {str(name) for (name,) in rows}


def months_ahead(present: set[str], day: date) -> int:
    """How many consecutive months after `day`'s month have a partition (-1 when `day`'s own
    month has none)."""
    first = month_start(day)
    if partition_name(first) not in present:
        return -1
    count = 0
    while partition_name(first := next_month(first)) in present:
        count += 1
    return count


def events_partitions(
    bind: Engine,
    day: date,
    notifier: Notifier | None = None,
    ahead: int = MONTHS_AHEAD,
) -> JobResult:
    """Job body for `events.partitions`: make sure the partitions ahead of `day` exist, then
    read back what exists. With fewer than `MIN_MONTHS_AHEAD` months ahead, or when the creation
    fails, it alerts `events.partitions_short` and fails (fixed code, no database text).

    Idempotent: a second run (or a re-run after a crash) creates nothing new. `rows` reports how
    many partitions are in place.
    """
    if bind.dialect.name != "postgresql":
        return JobResult(rows=0)
    notifier = notifier or LogNotifier()
    try:
        with bind.begin() as connection:
            names = ensure_event_partitions(connection, day, ahead)
            present = existing_partitions(connection)
    except SQLAlchemyError as error:
        notifier.alert(CODE_PARTITIONS_SHORT, day, {"reason": "create_failed"})
        raise JobError(CODE_PARTITIONS_SHORT) from error
    covered = months_ahead(present, day)
    if covered < MIN_MONTHS_AHEAD:
        notifier.alert(CODE_PARTITIONS_SHORT, day, {"months_ahead": covered})
        raise JobError(CODE_PARTITIONS_SHORT)
    logger.info("event partitions ensured: %d", len(names))
    return JobResult(rows=len(names))
