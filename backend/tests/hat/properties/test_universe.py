"""PBT universe.*: point-in-time membership, include/exclude and
the interval rules, on the real `app.hat.engine.portfolio.universe` against independent models."""

import uuid
from datetime import date, timedelta

from hypothesis import example, given
from hypothesis import strategies as st

from app.hat.data.reference import parse_industry_csv
from app.hat.engine.portfolio import universe
from app.hat.engine.portfolio.universe import Interval, UniverseError
from app.hat.engine.portfolio.universe_service import stock_rows

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


def intervals_valid(rows: list[Interval]) -> bool:
    """Every interval has effective_from before effective_to (when set), and the intervals of
    one instrument do not overlap (HAT-SCH-042)."""
    by_instrument: dict[uuid.UUID, list[Interval]] = {}
    for row in rows:
        if row.effective_to is not None and row.effective_from >= row.effective_to:
            return False
        by_instrument.setdefault(row.instrument_id, []).append(row)
    for items in by_instrument.values():
        ordered = sorted(items, key=lambda r: r.effective_from)
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if earlier.effective_to is None or earlier.effective_to > later.effective_from:
                return False
    return True


@given(OPS)
def test_membership_intervals(ops: list) -> None:
    """Whatever edits are applied or refused, every instrument's intervals stay ordered, non
    empty and non overlapping."""
    assert intervals_valid(apply_ops(ops))


def test_publish_universe_drops_excludes_first_and_keeps_stocks_only() -> None:
    """`publish-universe` resolves only the stock rows the excludes leave: an exclude drops its
    row whether or not an instrument matches it (NSE's `DUMMYHEG` placeholder), and a REIT's
    row is skipped and reported (decisions.md "Stocks only")."""
    text = (
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "ABB India Ltd.,Capital Goods,ABB,EQ,INE117A01022\n"
        "Dummy HEG Ltd.,Capital Goods,DUMMYHEG,EQ,DUM545A01024\n"
        "Bagmane Prime Office REIT,Realty,BAGMANE,RR,INE2OVN25015\n"
        "No ISIN Ltd.,Realty,NOISIN,EQ,\n"
    )
    rows = parse_industry_csv(text)
    kept, not_stocks = stock_rows(rows, frozenset({"DUMMYHEG"}))
    assert [r.symbol for r in kept] == ["ABB", "NOISIN"]
    assert not_stocks == ("BAGMANE",)
    kept, not_stocks = stock_rows(rows, frozenset())
    assert not_stocks == ("BAGMANE", "DUMMYHEG")
