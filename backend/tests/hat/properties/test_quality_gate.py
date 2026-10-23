"""PBT quality_gate.*: the nightly quality gate's pure checks (coverage, big moves, index
snapshot)."""

from fractions import Fraction

from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.hat.data.quality import (
    big_moves_check,
    coverage_check,
    index_snapshot_check,
)


@given(universe=st.integers(0, 6000), stored=st.integers(0, 6000))
@example(universe=100, stored=99)
@example(universe=100, stored=98)
@example(universe=3121, stored=3090)
@example(universe=3121, stored=3089)
def test_coverage_threshold(universe: int, stored: int) -> None:
    """For every universe and stored count, coverage passes iff stored / universe is at least
    0.99 by exact fractions (100 of 100 and 99 of 100 pass, 98 of 100 fails); an empty universe
    never passes."""
    expected = universe >= 1 and Fraction(stored, universe) >= Fraction(99, 100)
    assert coverage_check(universe, stored).passed == expected


@settings(max_examples=60)
@given(
    moved=st.sets(st.integers(0, 9)),
    explained=st.sets(st.integers(0, 9)),
    excluded=st.sets(st.integers(0, 9)),
    universe=st.sets(st.integers(0, 9)),
    size=st.integers(0, 1100),
)
@example(moved={1}, explained=set(), excluded=set(), universe=set(), size=100)
@example(moved={1, 2}, explained=set(), excluded=set(), universe=set(), size=199)
@example(moved={1, 2}, explained=set(), excluded=set(), universe=set(), size=200)
def test_big_moves_explained(
    moved: set[int], explained: set[int], excluded: set[int], universe: set[int], size: int
) -> None:
    """. For every set of instruments that moved over 20%, those with a
    corporate action on the day, those already excluded and every active-universe size N: the
    unexplained are the movers neither explained nor excluded; the check fails iff they are more
    than 1% of N (one of 100 passes, two of 199 fail, two of 200 pass); the day's exclusion list
    is the excluded plus, on a pass only, the unexplained."""
    check = big_moves_check(moved, explained, excluded, size)
    unexplained = moved - explained - excluded
    assert set(check.detail["unexplained"]) == {str(k) for k in unexplained}
    assert check.passed == (Fraction(len(unexplained)) <= Fraction(size, 100))
    out = excluded | (unexplained if check.passed else set())
    assert set(check.detail["excluded"]) == {str(k) for k in out}


@given(
    names=st.lists(
        st.sampled_from(
            ["NIFTY 50", "NIFTY 500", "Nifty 500", "INDIA VIX", "India Vix", "NIFTY BANK"]
        ),
        max_size=6,
    )
)
def test_index_snapshot_present(names: list[str]) -> None:
    """For every set of stored index names, the check passes iff NIFTY 500 and India VIX are both
    in it, whatever the case."""
    upper = {n.upper() for n in names}
    assert index_snapshot_check(names).passed == ({"NIFTY 500", "INDIA VIX"} <= upper)
