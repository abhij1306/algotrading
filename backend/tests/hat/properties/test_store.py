"""PBT store.*: atomic partition writes, immutable raw zips, one row per
(instrument, date) in `bars_daily`, and a missing day that stays missing. Real `DataStore` on a
temporary directory; no network."""

import hashlib
import random
import tempfile
import uuid
from datetime import date
from pathlib import Path

import polars as pl
import pytest
from hat_bhav_support import bar
from hypothesis import given, settings
from hypothesis import strategies as st

from app.hat.data.bhavcopy import weekdays
from app.hat.data.store import (
    DATASET_BARS,
    BarRow,
    DataStore,
    StoreError,
    latest_rows,
    partition_report,
)
from app.hat.ids import Uuid7Generator, uuid7

DAYS = weekdays(date(2024, 3, 1), date(2024, 4, 30))[:20]
prices = st.decimals(min_value="0.05", max_value="9999.95", places=2)


def _snapshot(root: Path) -> dict[str, str]:
    """Path to content digest (a failing comparison must not print or diff Parquet bytes)."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@given(
    ops=st.lists(
        st.tuples(st.sampled_from(["a.zip", "b.zip", "c.zip"]), st.binary(min_size=1, max_size=20)),
        min_size=1,
        max_size=12,
    )
)
def test_raw_immutable(ops: list[tuple[str, bytes]]) -> None:
    """Over any sequence of raw stores, a name keeps the bytes it was first given: the same bytes
    again is a no-op, different bytes raise and change nothing, and no raw file is removed."""
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        first: dict[str, bytes] = {}
        for name, data in ops:
            if name in first and first[name] != data:
                with pytest.raises(StoreError):
                    store.store_raw(name, data)
            else:
                assert store.store_raw(name, data) is (name not in first)
                first.setdefault(name, data)
            assert {n: store.read_raw(n) for n in first} == first
        assert sorted(p.name for p in (store.root / "raw").iterdir()) == sorted(first)


def test_raw_immutable_adjusted_values_not_storable() -> None:
    """Rows with a source other than a bhavcopy or MCP one (an adjusted value, say) are refused,
    as are names that would leave `raw/`."""
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        adjusted = [bar(uuid7(), uuid7(), source="adjusted")]
        with pytest.raises(StoreError):
            store.write_bars(DAYS[0], adjusted)
        for name in ("../x.zip", "a/b.zip", "", ".hidden"):
            with pytest.raises(StoreError):
                store.store_raw(name, b"x")


@st.composite
def run_plans(draw: st.DrawFn) -> list[dict]:
    ids = [uuid.UUID(int=10 + i) for i in range(3)]
    runs = draw(st.integers(1, 4))
    plan = []
    for _ in range(runs):
        cells = draw(
            st.dictionaries(
                st.tuples(st.sampled_from(DAYS[:5]), st.sampled_from(ids)), prices, max_size=8
            )
        )
        plan.append(cells)
    return plan


@given(plan=run_plans(), order=st.randoms(use_true_random=False))
@settings(max_examples=100)
def test_bars_daily_unique(plan: list[dict], order: random.Random) -> None:
    """For every set of raw partitions written by several runs in any order, `bars_daily` has no
    duplicate (instrument, date), and each row is the latest run's row for its key. A single write
    with a repeated key is refused."""
    gen = Uuid7Generator()
    run_ids = [gen.next() for _ in plan]
    jobs = list(range(len(plan)))
    order.shuffle(jobs)
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        for j in jobs:
            by_day: dict[date, list[BarRow]] = {}
            for (day, iid), price in plan[j].items():
                by_day.setdefault(day, []).append(bar(iid, run_ids[j], str(price)))
            for day, rows in by_day.items():
                store.write_bars(day, rows)
        con = store.connect()
        rows = con.execute(
            "SELECT instrument_id, date, close, ingest_run_id FROM bars_daily"
        ).fetchall()
        con.close()
        keys = [(i, d) for i, d, _, _ in rows]
        assert len(keys) == len(set(keys))
        expected = {}
        for j, cells in enumerate(plan):
            for (day, iid), price in cells.items():
                expected[(str(iid), day)] = (price, str(run_ids[j]))  # later run id wins
        assert {(i, d): (c, r) for i, d, c, r in rows} == expected
        if plan[0]:
            (day, iid), price = next(iter(plan[0].items()))
            twice = [bar(iid, run_ids[0], str(price))] * 2
            with pytest.raises(StoreError):
                store.write_bars(day, twice)


@given(
    written=st.sets(st.sampled_from(DAYS), max_size=len(DAYS)),
    expected=st.sets(st.sampled_from(DAYS), min_size=1),
)
def test_partitions_complete(written: set[date], expected: set[date]) -> None:
    """The report lists exactly the expected days with no rows, and extra days separately;
    reading a missing day returns nothing, never a copy of a neighbouring day."""
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        for n, day in enumerate(sorted(written)):
            store.write_bars(day, [bar(uuid.UUID(int=1), uuid7(), str(10 + n))])
        report = partition_report(store, DATASET_BARS, expected)
        assert report.missing == sorted(expected - written)
        assert report.unexpected == sorted(written - expected)
        assert set(report.row_counts) == written
        for day in expected - written:
            assert store.read_bars(day).height == 0
        assert sorted(store.days(DATASET_BARS)) == sorted(written)


def test_latest_rows_keeps_the_latest_run_per_day_in_read_order() -> None:
    d1, d2 = date(2024, 1, 1), date(2024, 1, 2)
    frame = pl.DataFrame(
        {
            "instrument_id": ["b", "a", "a", "a", "b"],
            "date": [d1, d1, d2, d1, d2],
            "ingest_run_id": ["r1", "r1", "r1", "r2", "r1"],
            "close": [1.0, 2.0, 3.0, 4.0, 5.0],
        }
    )
    assert latest_rows(frame)["close"].to_list() == [1.0, 3.0, 4.0, 5.0]
    unique = frame.filter(pl.col("ingest_run_id") == "r1")
    assert latest_rows(unique) is unique
