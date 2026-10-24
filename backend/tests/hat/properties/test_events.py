"""The event store's monthly partitions: each instant routes to exactly one month, and the
months created ahead. The Postgres side (identity ids, partitions, triggers) is checked by
`tests/hat/db`."""

from datetime import UTC, date, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from app.hat.ops.partitions import (
    month_of,
    month_start,
    months_to_cover,
    next_month,
    partition_bounds,
    partition_ddl,
    partition_name,
)

# --- partition routing (the arithmetic behind the DB check) ----------------------

instants = st.datetimes(
    min_value=datetime(2000, 1, 1), max_value=datetime(2200, 12, 31), timezones=st.just(UTC)
)
days = st.dates(min_value=date(2000, 1, 1), max_value=date(2200, 12, 31))


@given(instant=instants)
def test_partition_routing(instant: datetime) -> None:
    """Every instant is inside the half-open range of exactly one month's partition, and
    consecutive months' ranges meet with no gap and no overlap."""
    first = month_of(instant)
    lower, upper = partition_bounds(first)
    assert lower <= instant < upper
    assert partition_bounds(next_month(first))[0] == upper
    assert month_of(upper) == next_month(first)
    assert month_of(upper - timedelta(microseconds=1)) == first


@given(day=days, ahead=st.integers(min_value=0, max_value=5))
def test_partitions_cover_the_months_ahead(day: date, ahead: int) -> None:
    """The months to create for a day are its own month and the `ahead` following ones, in
    order, each with its own partition name and a statement that names only that partition."""
    months = months_to_cover(day, ahead)
    assert months[0] == month_start(day)
    assert len(months) == ahead + 1
    assert all(next_month(a) == b for a, b in zip(months, months[1:], strict=False))
    names = [partition_name(first) for first in months]
    assert len(set(names)) == len(names)
    for first, name in zip(months, names, strict=True):
        assert name.startswith("hat_event_p")
        assert partition_ddl(first).count(name) == 1
