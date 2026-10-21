"""PBT for the event store (HAT-SCH-062, 063, 064, 066, 067; HAT-DEC-I15) on the real
`app/hat/events.record`, over SQLite. The Postgres side (identity ids, partitions, triggers) is
checked by `tests/hat/db`."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from hat_support import event_engine
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from app.hat.events import (
    AGGREGATE_TYPES,
    EVENT_TYPES,
    TRADE_EVENT_TYPES,
    EventError,
    record,
)
from app.hat.models import HatEvent, HatPosition
from app.hat.ops.partitions import (
    month_of,
    month_start,
    months_to_cover,
    next_month,
    partition_bounds,
    partition_ddl,
    partition_name,
)

ENGINE = event_engine(HatPosition)
TYPES = sorted(EVENT_TYPES)
AGGREGATES = sorted(AGGREGATE_TYPES)
ACTOR_CHOICES = ("engine", "broker", "scheduler")


class BoomError(Exception):
    """A failure injected into an operation."""


def count_events(aggregate_id: uuid.UUID) -> int:
    with Session(ENGINE) as session:
        return session.scalar(
            select(func.count()).select_from(HatEvent).where(HatEvent.aggregate_id == aggregate_id)
        )


def stored_stop(position_id: uuid.UUID) -> Decimal:
    with Session(ENGINE) as session:
        return session.scalar(select(HatPosition.current_stop).where(HatPosition.id == position_id))


def new_position(stop: Decimal) -> uuid.UUID:
    position_id = uuid.uuid4()
    with Session(ENGINE) as session:
        session.execute(
            insert(HatPosition).values(
                id=position_id,
                bot_id=uuid.uuid4(),
                signal_id=uuid.uuid4(),
                instrument_id=uuid.uuid4(),
                is_virtual=True,
                qty_open=1,
                avg_entry=stop,
                initial_stop=Decimal("0.0001"),  # moves in this test go both ways
                current_stop=stop,
                currency="INR",
                opened_at=datetime(2026, 1, 1, tzinfo=UTC),
                state="Open",
                rules_fired={},
            )
        )
        session.commit()
    return position_id


money = st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("9999.9999"), places=4)
# Where an operation fails: never, between its two writes, after both and before the commit, or
# by an event the catalogue refuses; and whether the caller commits or rolls back what is left.
failure = st.sampled_from(["none", "before_state", "between", "before_commit", "bad_event"])


# --- HAT-SCH-062: a state change and its event commit or roll back together ------------------


@given(
    start=money,
    steps=st.lists(st.tuples(money, failure, st.booleans()), min_size=1, max_size=6),
)
def test_atomic_with_state(start: Decimal, steps: list[tuple[Decimal, str, bool]]) -> None:
    """For every sequence of stop moves, each failing at any point before the commit (or rolled
    back by its caller), a move and its `stop.moved` event are both stored or both absent."""
    position_id = new_position(start)
    signal_id = uuid.uuid4()
    expected_stop, expected_events = start, 0
    for new_stop, fail_at, commit in steps:
        session = Session(ENGINE)
        try:
            if fail_at == "before_state":
                raise BoomError
            session.execute(
                update(HatPosition)
                .where(HatPosition.id == position_id)
                .values(current_stop=new_stop)
            )
            if fail_at == "between":
                raise BoomError
            record(
                session,
                aggregate_type="position",
                aggregate_id=position_id,
                # a type the catalogue refuses fails after the state change was written
                event_type="stop.moved.typo" if fail_at == "bad_event" else "stop.moved",
                actor="engine",
                payload={"to": str(new_stop)},
                correlation_id=signal_id,
            )
            if fail_at == "before_commit":
                raise BoomError
            if commit:
                session.commit()
                expected_stop, expected_events = new_stop, expected_events + 1
            else:
                session.rollback()
        except (BoomError, EventError):
            session.rollback()
        finally:
            session.close()
        assert stored_stop(position_id) == expected_stop
        assert count_events(position_id) == expected_events


# --- HAT-SCH-063: only the catalogue ---------------------------------------------------------

near_misses = st.sampled_from(TYPES).flatmap(
    lambda name: st.sampled_from(
        [name.upper(), f" {name}", f"{name} ", f"{name}x", name.replace(".", "_"), name[:-1], ""]
    )
)
not_in_catalogue = st.one_of(st.text(max_size=30), near_misses).filter(
    lambda value: value not in EVENT_TYPES
)


@given(event_type=not_in_catalogue, aggregate_type=st.sampled_from(AGGREGATES))
def test_unknown_type_rejected(event_type: str, aggregate_type: str) -> None:
    """For every string outside the 35-type catalogue, `record()` raises and writes nothing."""
    aggregate_id = uuid.uuid4()
    with Session(ENGINE) as session:
        try:
            record(
                session,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
                actor="engine",
                payload={},
                correlation_id=aggregate_id,
            )
        except EventError:
            pass
        else:
            raise AssertionError(f"record() accepted the event type {event_type!r}")
        session.commit()
    assert count_events(aggregate_id) == 0


@given(event_type=st.sampled_from(TYPES))
def test_every_catalogue_type_is_recorded(event_type: str) -> None:
    """The catalogue is usable: each of its types records when given what the rules ask for."""
    aggregate_id = uuid.uuid4()
    with Session(ENGINE) as session:
        record(
            session,
            aggregate_type="signal",
            aggregate_id=aggregate_id,
            event_type=event_type,
            actor="engine",
            payload={},
            correlation_id=aggregate_id,
        )
        session.commit()
    assert count_events(aggregate_id) == 1


# --- HAT-SCH-064: aggregate types and correlation ---------------------------------------------

stream_item = st.tuples(
    st.sampled_from(TYPES),
    st.sampled_from(AGGREGATES + ["trade", "Signal", ""]),
    st.sampled_from(ACTOR_CHOICES),
    st.booleans(),  # a correlation id is given
    st.booleans(),  # the correlation id is the aggregate's own
    st.one_of(st.none(), st.integers(min_value=-2, max_value=40)),  # a causation id to try
)


@given(items=st.lists(stream_item, min_size=1, max_size=12))
def test_correlation(items: list[tuple]) -> None:
    """For every attempted stream of events, each stored event has a known aggregate type; every
    Signals, Orders and Positions event (watchlist.published aside) carries a correlation id,
    equal to the signal's own id on a signal aggregate; and every causation id names an earlier
    stored event."""
    stored: list[int] = []
    with Session(ENGINE) as session:
        for event_type, aggregate_type, actor, has_corr, own_corr, causation in items:
            aggregate_id = uuid.uuid4()
            correlation = (aggregate_id if own_corr else uuid.uuid4()) if has_corr else None
            try:
                stored.append(
                    record(
                        session,
                        aggregate_type=aggregate_type,
                        aggregate_id=aggregate_id,
                        event_type=event_type,
                        actor=actor,
                        payload={},
                        correlation_id=correlation,
                        causation_id=causation,
                    )
                )
            except EventError:
                continue
        session.commit()
    with Session(ENGINE) as session:
        rows = session.scalars(select(HatEvent).where(HatEvent.id.in_(stored))).all()
    assert len(rows) == len(stored)
    for row in sorted(rows, key=lambda r: r.id):
        assert row.aggregate_type in AGGREGATE_TYPES
        if row.event_type in TRADE_EVENT_TYPES:
            assert row.correlation_id is not None
            if row.aggregate_type == "signal":
                assert row.correlation_id == row.aggregate_id
        if row.causation_id is not None:
            assert row.causation_id < row.id
            with Session(ENGINE) as session:
                earlier = session.scalar(
                    select(func.count())
                    .select_from(HatEvent)
                    .where(HatEvent.id == row.causation_id)
                )
            assert earlier == 1


@given(bogus=st.integers(min_value=10**12, max_value=10**15))
def test_causation_must_exist(bogus: int) -> None:
    """A causation id no event has is refused, and nothing is written."""
    aggregate_id = uuid.uuid4()
    with Session(ENGINE) as session:
        try:
            record(
                session,
                aggregate_type="bot",
                aggregate_id=aggregate_id,
                event_type="bot.paused",
                actor="engine",
                payload={},
                causation_id=bogus,
            )
        except EventError:
            pass
        else:
            raise AssertionError("record() accepted an unknown causation id")
    assert count_events(aggregate_id) == 0


# --- HAT-SCH-067: ids increase ----------------------------------------------------------------

id_step = st.sampled_from(["commit", "rollback", "new_session"])


@given(steps=st.lists(id_step, min_size=1, max_size=25))
def test_ids_increasing(steps: list[str]) -> None:
    """For every sequence of `record()` calls, across sessions, commits and rollbacks, ids are
    unique and strictly increase in call order, and the ids that stay are in commit order."""
    issued: list[int] = []
    kept: list[int] = []
    session = Session(ENGINE)
    pending: list[int] = []
    try:
        for step in steps:
            issued.append(
                record(
                    session,
                    aggregate_type="bot",
                    aggregate_id=uuid.uuid4(),
                    event_type="bot.paused",
                    actor="engine",
                    payload={},
                )
            )
            pending.append(issued[-1])
            if step == "commit":
                session.commit()
                kept.extend(pending)
                pending = []
            elif step == "rollback":
                session.rollback()
                pending = []
            else:
                session.commit()
                kept.extend(pending)
                pending = []
                session.close()
                session = Session(ENGINE)
        session.commit()
        kept.extend(pending)
    finally:
        session.close()
    assert issued == sorted(set(issued))
    with Session(ENGINE) as check:
        stored = check.scalars(select(HatEvent.id).where(HatEvent.id.in_(issued))).all()
    assert sorted(stored) == sorted(kept)


# --- HAT-SCH-066: partition routing (the arithmetic behind the DB check) ----------------------

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


@given(
    start=st.integers(min_value=1, max_value=2000),
    count=st.integers(min_value=1, max_value=8),
)
def test_events_stay_after_a_failed_neighbour(start: int, count: int) -> None:
    """A refused event leaves the ones before it stored once the caller commits (nothing a
    failed `record()` wrote can sit in the session)."""
    aggregate_id = uuid.uuid4()
    with Session(ENGINE) as session:
        for _ in range(count):
            record(
                session,
                aggregate_type="bot",
                aggregate_id=aggregate_id,
                event_type="bot.resumed",
                actor="engine",
                payload={"n": start},
            )
            try:
                record(
                    session,
                    aggregate_type="bot",
                    aggregate_id=aggregate_id,
                    event_type="not.an.event",
                    actor="engine",
                    payload={},
                )
            except EventError:
                pass
        session.commit()
    assert count_events(aggregate_id) == count
