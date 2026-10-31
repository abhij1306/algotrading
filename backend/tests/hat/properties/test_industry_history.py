"""The industry history's pure seams: snapshot parsing, the taxonomy split, the old-label
mapping, the chain history, the row plan and the unknown member-days."""

from __future__ import annotations

import uuid
from datetime import date

from app.hat.data.industry_history import (
    ARCHIVED,
    Snapshot,
    archived_fill,
    best_fit,
    chain_history,
    old_mapping,
    parse_snapshot,
    plan_rows,
    snapshot_day,
    unknown_member_days,
)
from app.hat.data.instruments import chain_ids

OLD = Snapshot(
    date(2015, 3, 25),
    (("I1", "BANKS"), ("I2", "BANKS"), ("I3", "BANKS"), ("I9", "BANKS"), ("I8", "IT")),
)
NEW = Snapshot(
    date(2022, 5, 4),
    (
        ("I1", "Financial Services"),
        ("I2", "Financial Services"),
        ("I3", "Financial Services"),
        ("I4", "Power"),
    ),
)
NEWEST = Snapshot(
    date(2026, 10, 22),
    (("I1", "Financial Services"), ("I4", "Realty"), ("I5", "Power"), ("I6", "Chemicals")),
)
CHAIN = {f"I{n}": f"c{n}" for n in range(1, 10)} | {"I7": "c4"}  # I7 is c4's older ISIN


def test_parse_snapshot_skips_the_title_lines() -> None:
    text = (
        "﻿\nConstituents list of S&P CNX 500\n\n"
        "Company Name,Industry,Symbol,Series,ISIN Code\n\n"
        "ABB Ltd.,ELECTRICAL EQUIPMENT,ABB,EQ,INE117A01022\n"
        "No Isin Ltd.,POWER,NOISIN,EQ,\n"
    )
    assert parse_snapshot(text) == [("INE117A01022", "ELECTRICAL EQUIPMENT")]
    assert parse_snapshot("nothing here\n") == []
    assert snapshot_day("2026-10-22.csv") == date(2026, 10, 22)
    assert snapshot_day("notes.txt") is None


def test_old_labels_map_only_when_the_chains_agree() -> None:
    current = {"c1": "Financial Services", "c2": "Financial Services", "c3": "Financial Services"}
    assert old_mapping([{"c1": "BANKS", "c2": "BANKS", "c3": "BANKS"}], current) == {
        "BANKS": "Financial Services"
    }
    split = current | {"c3": "Realty"}
    assert old_mapping([{"c1": "BANKS", "c2": "BANKS", "c3": "BANKS"}], split) == {}
    assert old_mapping([{"c1": "BANKS", "c2": "BANKS"}], current) == {}  # too few to tell


def test_chain_history_back_dates_the_first_and_appends_changes() -> None:
    first_bar = {"c1": date(2011, 6, 22), "c4": date(2012, 1, 2), "c9": date(2013, 5, 6)}
    history, mapping = chain_history([OLD, NEW, NEWEST], CHAIN, first_bar)
    assert mapping == {"BANKS": "Financial Services"}
    assert history["c1"] == [(date(2011, 6, 22), "Financial Services")]
    assert history["c4"] == [(date(2012, 1, 2), "Power"), (date(2026, 10, 22), "Realty")]
    assert history["c5"] == [(date(2026, 10, 22), "Power")]  # no bar: from its snapshot
    assert history["c9"] == [(date(2013, 5, 6), "Financial Services")]  # old list, mapped
    assert "c8" not in history  # IT maps to nothing: unknown


def test_best_fit_takes_the_most_chains_label_and_leaves_ties_out() -> None:
    current = {"c1": "FMCG", "c2": "FMCG", "c3": "Durables", "c4": "Power", "c5": "Oil"}
    old = [{"c1": "CONSUMER", "c2": "CONSUMER", "c3": "CONSUMER", "c4": "ENERGY", "c5": "ENERGY"}]
    assert best_fit(old, current) == {"CONSUMER": ("FMCG", 2, 3)}  # ENERGY: two votes, a tie


def test_archived_fill_dates_each_list_and_skips_classified_chains() -> None:
    """An unknown chain takes each pre-2016 list's best-fit label from that list's day; a
    classified chain, a 2016+ list and a label that maps to nothing add no row."""
    old10 = Snapshot(date(2010, 1, 2), (("I1", "IT"), ("I2", "IT"), ("I3", "IT"), ("I8", "IT")))
    old14 = Snapshot(
        date(2014, 1, 22),
        (("I1", "IT"), ("I2", "IT"), ("I3", "IT"), ("I8", "MISC"), ("I9", "IT")),
    )
    old18 = Snapshot(date(2018, 10, 4), (("I5", "IT"),))
    now = Snapshot(
        date(2026, 10, 22),
        (("I1", "Information Technology"), ("I2", "Information Technology"), ("I3", "Power")),
    )
    fill, fit = archived_fill([old10, old14, old18, now], CHAIN, {"c1", "c2", "c3", "c9"})
    assert fit == {"IT": ("Information Technology", 2, 3)}
    assert fill == {"c8": [(date(2010, 1, 2), "Information Technology")]}  # MISC: no row


def test_plan_rows_replaces_the_managed_rows_only() -> None:
    target = {
        "c1": [(date(2011, 6, 22), "Power", None)],
        "c4": [(date(2012, 1, 2), "Power", None)],
        "c8": [(date(2014, 1, 22), "Power", ARCHIVED)],
    }
    stored = [
        ("c1", "c1", date(2026, 10, 22), "Power", None),  # the load-day row: replaced
        ("c4", "c4", date(2012, 1, 2), "Power", None),  # already right
        ("c4", "c4", date(2026, 11, 2), "Realty", None),  # after the newest snapshot: kept
        ("c3", "c3", date(2026, 10, 22), "Realty", None),  # a chain the snapshots do not classify
        ("i7", "c4", date(2020, 1, 1), "Power", None),  # on an older ISIN: moved to the head
        ("c8", "c8", date(2014, 1, 22), "Power", None),  # its source changed: rewritten
    ]
    inserts, deletes, kept = plan_rows(target, stored, date(2026, 10, 25))
    assert inserts == [
        ("c1", date(2011, 6, 22), "Power", None),
        ("c8", date(2014, 1, 22), "Power", ARCHIVED),
    ]
    assert deletes == [
        ("c1", date(2026, 10, 22)),
        ("c8", date(2014, 1, 22)),
        ("i7", date(2020, 1, 1)),
    ]
    assert kept == 3
    again = [
        ("c1", "c1", date(2011, 6, 22), "Power", None),
        *stored[1:4],
        ("c8", "c8", date(2014, 1, 22), "Power", ARCHIVED),
    ]
    assert plan_rows(target, again, date(2026, 10, 25)) == ([], [], 5)


def test_unknown_member_days() -> None:
    sessions = [date(2024, 1, d) for d in range(1, 11)]
    spans = {"a": [(date(2024, 1, 1), date(2024, 1, 6))], "b": [(date(2024, 1, 3), None)]}
    history = {"a": [(date(2024, 1, 4), "Power")]}
    total, unknown, per_chain = unknown_member_days(sessions, spans, history)
    assert (total, unknown, per_chain) == (13, 11, {"a": 3, "b": 8})


def test_chain_ids_name_every_isin_of_the_chain() -> None:
    """The live entry route reads a chain's industry by all its ids (CodeAnt #284)."""
    old, new, alone = (uuid.UUID(int=n) for n in (1, 2, 3))
    members = {str(old): frozenset({str(old), str(new)}), str(new): frozenset({str(old), str(new)})}
    assert chain_ids(members, new) == [old, new]
    assert chain_ids(members, alone) == [alone]
