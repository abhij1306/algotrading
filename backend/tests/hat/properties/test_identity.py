"""One instrument per ISIN (audit D5): the pure rules of `data/identity.py` and the row choice of
the Parquet re-key."""

from __future__ import annotations

import uuid
from datetime import date

from app.hat.data.identity import (
    Interval,
    Sibling,
    best_per_isin,
    kept_id,
    merge_symbol_history,
    series_changes,
    series_on,
    series_rank,
)
from app.hat.data.isin_merge import coverage_ids, keep_rows


def test_series_rank_puts_the_normal_market_first() -> None:
    assert sorted(["BL", "IL", "BZ", "BE", "EQ", "BT"], key=series_rank) == [
        "EQ",
        "BE",
        "BZ",
        "BL",
        "BT",
        "IL",
    ]


def test_best_per_isin_keeps_the_market_row_of_each_isin() -> None:
    rows = [("A", "BL"), ("B", "BE"), ("A", "EQ"), ("C", "IL"), ("B", "BL")]
    kept = best_per_isin(rows, lambda r: r[0], lambda r: r[1])
    assert kept == [("A", "EQ"), ("B", "BE"), ("C", "IL")]


def test_kept_id_prefers_active_then_series_then_lowest_id() -> None:
    low, high = uuid.UUID(int=1), uuid.UUID(int=2)
    assert kept_id([Sibling(low, "EQ", False), Sibling(high, "BE", True)]) == high
    assert kept_id([Sibling(high, "EQ", False), Sibling(low, "BE", False)]) == high
    assert kept_id([Sibling(high, "EQ", False), Sibling(low, "EQ", False)]) == low


def test_merge_keeps_a_rename_recorded_on_the_sibling() -> None:
    # The EQ row was created by a refresh after the move and claims SYM from the listing date;
    # the BE row it replaced holds the rename OLD -> SYM.
    eq = [Interval("SYM", date(2005, 1, 3), None)]
    be = [
        Interval("OLD", date(2005, 1, 3), date(2020, 6, 1)),
        Interval("SYM", date(2020, 6, 1), date(2024, 5, 4)),
    ]
    assert merge_symbol_history([eq, be]) == [
        Interval("OLD", date(2005, 1, 3), date(2020, 6, 1)),
        Interval("SYM", date(2020, 6, 1), None),
    ]


def test_merge_joins_consecutive_series_and_keeps_gaps() -> None:
    eq = [Interval("SYM", date(2012, 1, 2), date(2015, 3, 2))]
    be = [Interval("SYM", date(2015, 3, 2), date(2016, 1, 1))]
    later = [Interval("SYM", date(2018, 1, 1), None)]
    assert merge_symbol_history([later, eq, be]) == [
        Interval("SYM", date(2012, 1, 2), date(2016, 1, 1)),
        Interval("SYM", date(2018, 1, 1), None),
    ]


def test_merge_of_one_history_is_that_history() -> None:
    one = [
        Interval("A", date(2010, 1, 1), date(2012, 1, 1)),
        Interval("B", date(2012, 1, 1), None),
    ]
    assert merge_symbol_history([one]) == one


def test_keep_rows_one_per_instrument_best_series() -> None:
    ids = ["k", "k", "x", "k"]
    series = ["BL", "EQ", "BE", "IL"]
    assert keep_rows(ids, series) == [1, 2]


def test_coverage_ids_map_and_refuse_a_non_uuid() -> None:
    old, kept = str(uuid.UUID(int=3)), str(uuid.UUID(int=4))
    assert coverage_ids([old, kept], {old: kept}) == [kept]
    try:
        coverage_ids(["../x"], {})
    except ValueError:
        return
    raise AssertionError("a non-uuid id was accepted")


def test_series_on_reads_the_series_of_its_day() -> None:
    d = date(2024, 1, 1)
    rows = [("x", date(2024, 1, 3), "BE"), ("x", d, "EQ"), ("x", date(2024, 1, 2), "EQ")]
    changes = series_changes(rows)
    assert changes == {"x": [(d, "EQ"), (date(2024, 1, 3), "BE")]}
    assert series_on(changes, "x", date(2023, 12, 31)) is None
    assert series_on(changes, "x", date(2024, 1, 2)) == "EQ"
    assert series_on(changes, "x", date(2024, 1, 9)) == "BE"
    assert series_on(changes, "y", d) is None
