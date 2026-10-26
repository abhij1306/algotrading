"""The point-in-time index membership's pure seams: undoing change notices from today's list,
the count check and the CSV parser (`data/index_membership.py`)."""

from __future__ import annotations

from datetime import date

import pytest

from app.hat.data.index_membership import (
    Change,
    IndexMembershipError,
    Member,
    member_counts,
    off_size,
    parse_membership_csv,
    undo_changes,
)

START, AS_OF = date(2016, 1, 1), date(2026, 10, 23)
TODAY = "ind_nifty500list.csv"


def _change(day: date, added: dict[str, str], removed: dict[str, str], source: str) -> Change:
    return Change(day, added, removed, source)


def test_undo_rebuilds_intervals_back_from_today() -> None:
    """Undoing a replacement makes the added stock a member from its effective date and the
    removed one a member until it; a stock that left and came back has two intervals."""
    current = {"A": "A", "B": "B", "C": "CNEW"}
    changes = [
        _change(date(2020, 3, 27), {"B": "B"}, {"X": "X"}, "n2.pdf"),
        _change(date(2018, 4, 2), {"X": "X"}, {"B": "B"}, "n1.pdf"),
    ]
    rows = undo_changes(current, AS_OF, changes, START, TODAY)
    assert sorted((m.key, m.effective_from, m.effective_to, m.source) for m in rows) == [
        ("A", START, None, TODAY),
        ("B", START, date(2018, 4, 2), "n1.pdf"),
        ("B", date(2020, 3, 27), None, "n2.pdf"),
        ("C", START, None, TODAY),
        ("X", date(2018, 4, 2), date(2020, 3, 27), "n1.pdf;n2.pdf"),
    ]
    assert member_counts(((m.effective_from, m.effective_to) for m in rows), [START]) == {START: 3}


@pytest.mark.parametrize(
    ("added", "removed"),
    [
        ({"Z": "Z"}, {}),  # adds a stock today's list does not hold
        ({}, {"A": "A"}),  # removes one that is still a member after the change
        ({"A": "A"}, {"A": "A"}),  # both sides
    ],
)
def test_undo_refuses_a_change_that_does_not_fit(
    added: dict[str, str], removed: dict[str, str]
) -> None:
    changes = [_change(date(2020, 1, 1), added, removed, "n")]
    with pytest.raises(IndexMembershipError, match="membership_undo_conflict"):
        undo_changes({"A": "A"}, AS_OF, changes, START, TODAY)


def test_undo_refuses_a_change_outside_the_cover() -> None:
    late = _change(date(2026, 10, 30), {"A": "A"}, {"B": "B"}, "n")
    with pytest.raises(IndexMembershipError, match="membership_change_outside_cover"):
        undo_changes({"A": "A"}, AS_OF, [late], START, TODAY)


def test_off_size_lists_the_change_dates_whose_count_is_not_the_size() -> None:
    """A temporary vacancy (one out on day 2, its replacement in on day 3) shows on day 2 only."""
    d1, d2, d3 = date(2020, 1, 1), date(2020, 2, 1), date(2020, 3, 1)
    rows = [(d1, None), (d1, d2), (d3, None)]
    assert off_size(rows, 2, AS_OF) == {d2: 1}
    assert off_size(rows, 2, date(2020, 2, 15)) == {d2: 1}
    assert off_size([(d1, None), (d1, None)], 2, AS_OF) == {}


def test_parse_reads_one_index() -> None:
    text = (
        "index_name,isin,symbol_at_the_time,from_date,to_date,source_notice\n"
        "Nifty 500,ine002a01018,RELIANCE,2016-01-01,,start\n"
        "NIFTY 500,INE001A01036,HDFC,2016-01-01,2023-07-13,ind_prs04072023.pdf\n"
    )
    name, rows = parse_membership_csv(text)
    assert name == "NIFTY 500"
    assert rows == [
        Member("INE002A01018", "RELIANCE", date(2016, 1, 1), None, "start"),
        Member("INE001A01036", "HDFC", date(2016, 1, 1), date(2023, 7, 13), "ind_prs04072023.pdf"),
    ]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("NIFTY 500,X,S,2016-01-01,2016-01-01,n", "membership_csv_empty_interval@2"),
        ("NIFTY 500,X,S,2016-13-01,,n", "membership_bad_date@2"),
        ("NIFTY 500,,S,2016-01-01,,n", "membership_csv_empty_field@2"),
        ("NIFTY 500,X,S,2016-01-01,,n\nNIFTY 50,Y,T,2016-01-01,,n", "membership_csv_mixed_index"),
        ("", "membership_csv_empty"),
    ],
)
def test_parse_refuses_a_bad_file(body: str, code: str) -> None:
    header = "index_name,isin,symbol_at_the_time,from_date,to_date,source_notice\n"
    with pytest.raises(IndexMembershipError) as raised:
        parse_membership_csv(header + body)
    assert raised.value.code == code
