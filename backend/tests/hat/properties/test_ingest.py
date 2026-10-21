"""PBT ingest.event_and_state_atomic (HAT-DEC-I15): an ingest outcome and its event are written
by one transaction. The nightly ingest (PR 7) records `ingest.completed`, `ingest.failed` and
`quality.failed` with `events.record`; this drives the same recording with a job-run row as the
state, failing at every point before the commit."""

import dataclasses
import hashlib
import tempfile
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
from hat_support import event_engine
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.hat.data.bhavcopy import weekdays
from app.hat.data.history_build import INDEX_NAMES, IndexPoint, load_index_history
from app.hat.data.store import BAR_SCHEMA, BarRow, DataStore, IndexRow, index_coverage
from app.hat.events import EventError, record
from app.hat.ids import uuid7
from app.hat.models import HatEvent, HatJobRun

ENGINE = event_engine(HatJobRun)

outcomes = st.sampled_from(["ingest.completed", "ingest.failed", "quality.failed"])
fail_points = st.sampled_from(["none", "after_state", "after_event", "bad_event", "rollback"])


class InjectedError(Exception):
    """A failure injected between two writes."""


@given(runs=st.lists(st.tuples(outcomes, fail_points), min_size=1, max_size=8))
def test_event_and_state_atomic(runs: list[tuple[str, str]]) -> None:
    """For every sequence of ingest outcomes, each failing at any point before its commit, the
    job-run row and the outcome event exist together or not at all."""
    committed: dict[str, str] = {}
    for number, (event_type, fail_at) in enumerate(runs):
        job = f"ingest.nightly.{uuid.uuid4().hex}"
        run_day = date(2026, 1, 1 + number)
        run_id = uuid.uuid4()
        session = Session(ENGINE)
        try:
            session.add(
                HatJobRun(
                    id=run_id,
                    job=job,
                    trade_date=run_day,
                    started_at=datetime(2026, 1, 1, tzinfo=UTC),
                    status="failed" if event_type != "ingest.completed" else "succeeded",
                    calls=1,
                    rows=1,
                )
            )
            session.flush()
            if fail_at == "after_state":
                raise InjectedError
            record(
                session,
                aggregate_type="ingest",
                aggregate_id=run_id,
                event_type="ingest.typo" if fail_at == "bad_event" else event_type,
                actor="scheduler",
                payload={"trade_date": str(run_day)},
            )
            if fail_at == "after_event":
                raise InjectedError
            if fail_at == "rollback":
                session.rollback()
            else:
                session.commit()
                committed[job] = event_type
        except (InjectedError, EventError):
            session.rollback()
        finally:
            session.close()
        with Session(ENGINE) as check:
            rows = check.scalar(
                select(func.count()).select_from(HatJobRun).where(HatJobRun.job == job)
            )
            events = check.scalars(
                select(HatEvent.event_type).where(HatEvent.aggregate_id == run_id)
            ).all()
        stored = 1 if job in committed else 0
        assert rows == stored
        assert len(events) == stored
        if job in committed:
            assert events == [committed[job]]


# --- PR 5: the Parquet store and the index history (HAT-DEC-T6, HAT-DATA-011, 085, 095) -----------

BAR_DAY = date(2024, 5, 6)
money = st.decimals(min_value="0", max_value="99999999.9999", places=4)


@st.composite
def bar_sets(draw: st.DrawFn) -> list[BarRow]:
    run = uuid7()
    n = draw(st.integers(1, 8))
    return [
        BarRow(
            uuid.UUID(int=i + 1),
            f"INE00000000{i}",
            f"SYM{i}",
            draw(st.sampled_from(["EQ", "BE"])),
            draw(money),
            draw(money),
            draw(money),
            draw(money),
            draw(money),
            draw(st.integers(0, 10**10)),
            draw(st.one_of(st.none(), money)),
            draw(st.sampled_from(["bhavcopy", "mcp"])),
            run,
        )
        for i in range(n)
    ]


def _tuple(r: BarRow) -> tuple:
    return (
        str(r.instrument_id),
        r.isin,
        r.symbol,
        r.series,
        r.open,
        r.high,
        r.low,
        r.close,
        r.prev_close,
        r.volume,
        r.traded_value,
        r.source,
        str(r.ingest_run_id),
    )


@given(rows=bar_sets())
def test_parquet_roundtrip(rows: list[BarRow]) -> None:
    """For every set of bar rows written for a date, reading the date back through DuckDB (and
    through polars) returns the same rows with the same values, exactly."""
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        store.write_bars(BAR_DAY, rows)
        con = store.connect()
        got = con.execute(
            "SELECT instrument_id, isin, symbol, series, open, high, low, close, prev_close, "
            "volume, traded_value, source, ingest_run_id FROM bars_daily_raw "
            "WHERE date = ? ORDER BY instrument_id",
            [BAR_DAY],
        ).fetchall()
        con.close()
        frame = store.read_bars(BAR_DAY).sort("instrument_id")
    wanted = sorted((_tuple(r) for r in rows), key=lambda t: t[0])
    assert got == wanted
    assert [tuple(r.values()) for r in frame.to_dicts()] == wanted


@given(rows=bar_sets(), other=st.sampled_from(["bhavcopy", "mcp"]))
def test_sources_same_schema(rows: list[BarRow], other: str) -> None:
    """Rows from either source land in files with the identical column set and types, equal
    except `source`, `ingest_run_id` and `traded_value` (null for MCP), so the nightly ingest and
    the historical build write one dataset. (The MCP normaliser itself arrives in PR 7; this
    drives the shared writer both sources use.)"""
    run = uuid7()
    mcp = [dataclasses.replace(r, traded_value=None, source="mcp", ingest_run_id=run) for r in rows]
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        first = store.write_bars(BAR_DAY, rows)
        second = store.write_bars(BAR_DAY + timedelta(days=1), mcp)
        assert pq.read_schema(first).remove_metadata().equals(BAR_SCHEMA)
        assert pq.read_schema(second).remove_metadata().equals(BAR_SCHEMA)
        a = store.read_bars(BAR_DAY).drop("source", "ingest_run_id", "traded_value")
        b = store.read_bars(BAR_DAY + timedelta(days=1)).drop(
            "source", "ingest_run_id", "traded_value"
        )
    assert a.sort("instrument_id").equals(b.sort("instrument_id"))
    assert all(r.traded_value is None for r in mcp)


def _digest(path: Path) -> str:
    """Short hash for comparisons, so a failure never prints (or diffs) Parquet bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


INDEX_DAYS = weekdays(date(2024, 1, 1), date(2024, 3, 29))[:30]


@st.composite
def index_loads(draw: st.DrawFn) -> tuple[set, int]:
    """Which (index, day) points exist in the source, with repeated chunks overlapping."""
    present = {
        (name, day) for name in INDEX_NAMES for day in INDEX_DAYS if draw(st.integers(0, 6)) > 0
    }
    repeats = draw(st.integers(1, 3))
    return present, repeats


@given(load=index_loads())
@settings(max_examples=60)
def test_index_snapshot_stored(load: tuple[set, int]) -> None:
    """After loading any index history (overlapping chunks included), `index_daily` has exactly
    one row per index per source day, each with the loaded values, and the coverage report names
    precisely the trading days with no row; a repeated load changes nothing."""
    present, repeats = load

    def source(name: str, start: date, end: date) -> Iterator[IndexPoint]:
        for _ in range(repeats):
            for day in INDEX_DAYS:
                if (name, day) in present:
                    value = Decimal(INDEX_DAYS.index(day) + 10)
                    yield IndexPoint(day, IndexRow(name, value, value, value, value))

    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        load_index_history(store, source, INDEX_DAYS[0], INDEX_DAYS[-1])
        snapshot = {p: _digest(p) for p in Path(tmp).rglob("*.parquet")}
        load_index_history(store, source, INDEX_DAYS[0], INDEX_DAYS[-1])
        assert {p: _digest(p) for p in Path(tmp).rglob("*.parquet")} == snapshot
        con = store.connect()
        rows = con.execute("SELECT index_name, date, close FROM index_daily").fetchall()
        con.close()
        # the same points arriving one index at a time must merge, not replace, a day's file
        piecewise = DataStore(Path(tmp) / "piecewise")
        for name, day in sorted(present):
            value = Decimal(INDEX_DAYS.index(day) + 10)
            piecewise.upsert_index(day, [IndexRow(name, value, value, value, value)])
        pcon = piecewise.connect()
        piece_rows = pcon.execute("SELECT index_name, date, close FROM index_daily").fetchall()
        pcon.close()
        assert sorted(piece_rows) == sorted(rows)
        starts = dict.fromkeys(INDEX_NAMES, INDEX_DAYS[0])
        missing = index_coverage(store, set(INDEX_DAYS), starts)
    assert sorted((n, d) for n, d, _ in rows) == sorted(present)
    assert all(c == Decimal(INDEX_DAYS.index(d) + 10) for _, d, c in rows)
    for name in INDEX_NAMES:
        assert missing[name] == [d for d in INDEX_DAYS if (name, d) not in present]
