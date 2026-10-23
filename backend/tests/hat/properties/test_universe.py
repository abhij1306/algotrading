"""PBT universe.*: point-in-time membership, include/exclude and
the interval rules, on the real `app.hat.engine.portfolio.universe` against independent models."""

import uuid
from datetime import UTC, date, datetime, time, timedelta

from hat_support import memory_engine
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.engine.portfolio import service, universe
from app.hat.engine.portfolio.universe import Interval, UniverseError
from app.hat.models import HatMarket, HatUniverseVersion

POOL = tuple(uuid.UUID(int=i + 1) for i in range(5))
START = date(2026, 1, 1)
DAYS = st.integers(0, 40).map(lambda n: START + timedelta(days=n))
INSTRUMENTS = st.sampled_from(POOL)
OPS = st.lists(
    st.tuples(st.sampled_from(("add", "remove")), INSTRUMENTS, DAYS), min_size=0, max_size=25
)


def apply_ops(ops: list) -> list[Interval]:
    rows: list[Interval] = []
    for op, instrument, day in ops:
        try:
            rows = (universe.add_member if op == "add" else universe.remove_member)(
                rows, instrument, day
            )
        except UniverseError:
            pass
    return rows


@st.composite
def valid_intervals(draw: st.DrawFn) -> list[Interval]:
    rows: list[Interval] = []
    for instrument in POOL:
        cursor = draw(st.integers(0, 10))
        for _ in range(draw(st.integers(0, 3))):
            length = draw(st.one_of(st.none(), st.integers(1, 12)))
            first = START + timedelta(days=cursor)
            if length is None:
                rows.append(Interval(instrument, first, None))
                break
            rows.append(Interval(instrument, first, first + timedelta(days=length)))
            cursor += length + draw(st.integers(0, 6))
    return rows


@given(valid_intervals(), DAYS)
def test_point_in_time_members(rows: list[Interval], day: date) -> None:
    """The members on a date are exactly the instruments with an interval [from, to) holding it,
    ended (delisted) members included for the dates they were in."""
    expected = {
        r.instrument_id
        for r in rows
        if r.effective_from <= day and (r.effective_to is None or day < r.effective_to)
    }
    assert universe.members_on(rows, day) == expected


@given(OPS, DAYS, DAYS)
def test_membership_history_is_append_only(ops: list, day: date, later: date) -> None:
    """An edit never changes who was a member before its date: the membership of every earlier
    date is the same after it (so a backtest of the past does not move)."""
    rows = apply_ops(ops)
    probe = min(day, later)
    before = universe.members_on(rows, probe)
    edit_day = max(day, later)
    for instrument in POOL:
        for change in (universe.add_member, universe.remove_member):
            try:
                after = change(rows, instrument, edit_day)
            except UniverseError:
                continue
            if probe < edit_day:
                assert universe.members_on(after, probe) == before


@given(st.sets(INSTRUMENTS), st.sets(INSTRUMENTS), st.sets(INSTRUMENTS))
@example(constituents={POOL[0]}, include=set(), exclude={POOL[0]})
def test_include_exclude(constituents: set, include: set, exclude: set) -> None:
    """The MVP universe is (constituents + includes) minus excludes: an exclude removes an
    instrument whatever added it, an include adds one the index lacks, and nothing else enters."""
    result = universe.effective_members(constituents, include, exclude)
    assert result == {i for i in POOL if (i in constituents or i in include) and i not in exclude}
    assert not result & exclude
    assert result <= constituents | include


@given(OPS, st.sets(INSTRUMENTS), DAYS)
def test_sync_reaches_the_target(ops: list, target: set, day: date) -> None:
    """Applying `plan_sync` on a date makes the members of that date equal the target, without
    touching the dates before it."""
    rows = apply_ops(ops)
    to_add, to_remove = universe.plan_sync(rows, target, day)
    assert not to_add & to_remove
    try:
        for instrument in sorted(to_remove, key=str):
            rows_after = universe.remove_member(rows, instrument, day)
            rows = rows_after
        for instrument in sorted(to_add, key=str):
            rows = universe.add_member(rows, instrument, day)
    except UniverseError:
        return  # the date reaches into recorded history: refused, nothing half-applied
    assert universe.members_on(rows, day) == target


@given(OPS)
def test_membership_intervals(ops: list) -> None:
    """Whatever edits are applied or refused, every instrument's intervals stay ordered, non
    empty and non overlapping."""
    assert universe.intervals_valid(apply_ops(ops))


@given(st.sampled_from(POOL), DAYS, st.integers(-5, 5), st.integers(0, 5))
def test_membership_intervals_detects_bad_rows(
    instrument: uuid.UUID, day: date, length: int, gap: int
) -> None:
    """An empty or reversed interval, or two intervals of one instrument that overlap, are not
    valid."""
    end = day + timedelta(days=length)
    assert universe.intervals_valid([Interval(instrument, day, end)]) == (length > 0)
    first = Interval(instrument, day, day + timedelta(days=3))
    second = Interval(instrument, day + timedelta(days=3 - gap), None)
    assert universe.intervals_valid([first, second]) == (gap == 0)


@given(st.sets(INSTRUMENTS), st.sets(INSTRUMENTS), st.sets(INSTRUMENTS))
def test_leave_no_force_sell(before: set, after: set, open_positions: set) -> None:
    """Leaving the universe blocks new entries and never forces a sale: a position in a
    departed instrument keeps its own exit, and no exit is ever ordered by the change."""
    change = universe.membership_change(before, after, open_positions)
    assert change.forced_exits == frozenset()
    assert change.blocked_entries == before - after
    assert change.running_to_exit == (before - after) & open_positions
    entered = universe.entries_allowed(POOL, after)
    assert not set(entered) & (before - after)
    assert set(entered) == set(POOL) & after


@settings(max_examples=25)
@given(
    st.sets(INSTRUMENTS),
    st.sets(INSTRUMENTS),
    st.sets(INSTRUMENTS),
    st.sets(INSTRUMENTS),
    DAYS,
)
def test_seeded_version_is_seen_only_once_published(
    constituents: set, include: set, exclude: set, later: set, as_of: date
) -> None:
    """A seeded version is a draft that records its member list and changes nothing the
    universe's members show; publishing it makes the members of its date the (constituents +
    includes - excludes) set, retires the version that was live, and leaves exactly one live."""
    engine = memory_engine()
    with Session(engine) as session:
        market = HatMarket(
            code="NSE",
            timezone="Asia/Kolkata",
            currency="INR",
            session_open=time(9, 15),
            session_close=time(15, 30),
        )
        session.add(market)
        session.flush()
        uni = service.create_universe(
            session, user_id=uuid.uuid4(), market_id=market.id, name="mvp"
        )
        first = service.seed_universe_version(
            session, uni.id, constituents=constituents, as_of=as_of
        )
        assert service.members_of(session, uni.id, as_of) == frozenset()
        service.publish_version(session, first.id, datetime(2026, 1, 1, tzinfo=UTC))
        assert service.members_of(session, uni.id, as_of) == frozenset(constituents)
        second = service.seed_universe_version(
            session,
            uni.id,
            constituents=later,
            include=include,
            exclude=exclude,
            as_of=as_of + timedelta(days=3),
        )
        target = universe.effective_members(later, include, exclude)
        probe = as_of + timedelta(days=3)
        assert service.members_of(session, uni.id, probe) == frozenset(constituents)
        service.publish_version(session, second.id, datetime(2026, 1, 2, tzinfo=UTC))
        assert service.members_of(session, uni.id, probe) == target
        assert service.members_of(session, uni.id, as_of) == frozenset(constituents)  # history
        statuses = sorted(v.status for v in session.scalars(select(HatUniverseVersion)).all())
        assert statuses == ["live", "retired"]
        assert [
            v.version
            for v in session.scalars(
                select(HatUniverseVersion).order_by(HatUniverseVersion.version)
            )
        ] == [1, 2]
