"""PBT ingest.* (..045, 048, 049; ): the MCP client's batching, concurrency and date check, the
nightly ingest (probe,
universe, row checks, raw write-once, outcome event, call budget, index and breadth), symbol
renames from the stored bars, and the Parquet store both sources write.

`ingest.event_and_state_atomic` is a check: an ingest outcome and its event are written by
one transaction; the nightly ingest's own hook is checked by `ingest.outcome_event`."""

import dataclasses
import hashlib
import math
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from hat_ingest_support import (
    INDEX_ROWS,
    TRADE_DAY,
    Crash,
    Quote,
    World,
    bhav_day,
    day_at,
    make_world,
    sleeper,
    store_previous,
    symbols_of,
)
from hat_mcp_support import ScriptedTransport, reference_engine, seed_market
from hat_support import event_engine
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.hat.clock import FixedClock
from app.hat.data.bhavcopy import weekdays
from app.hat.data.history_build import INDEX_NAMES, IndexPoint, load_index_history
from app.hat.data.ingest import (
    CODE_NO_UNIVERSE,
    check_quote,
    ingest_body,
    outcome_recorder,
    run_ingest,
    wait_for_data,
)
from app.hat.data.instruments import resolve_symbol, seed_symbol_history
from app.hat.data.mcp_client import McpClient, batches
from app.hat.data.renames import renames_from_bars
from app.hat.data.store import (
    BAR_SCHEMA,
    DATASET_BARS,
    DATASET_MARKET_BREADTH,
    BarRow,
    DataStore,
    IndexRow,
    index_coverage,
)
from app.hat.events import EventError, record
from app.hat.ids import uuid7
from app.hat.models import HatEvent, HatInstrument, HatJobRun
from app.hat.ops.jobs import JobError, run_job
from app.hat.risk.controls import kill_rows
from app.hat.risk.kill import TRIPPED, current_state

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
            assert events[0] == committed[job]


# --- the Parquet store and the index history -----------

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


symbols_list = st.lists(
    st.from_regex(r"[A-Z][A-Z0-9&-]{0,9}", fullmatch=True), unique=True, max_size=300
)


@given(n=st.integers(0, 3300))
@example(n=3121)
def test_batches_le_50(n: int) -> None:
    """For every list of n symbols the batches partition it in order, each holds at most 50, and
    there are ceil(n / 50) of them (63 for 3,121)."""
    items = [f"S{i}" for i in range(n)]
    parts = batches(items)
    assert [s for part in parts for s in part] == items
    assert all(1 <= len(part) <= 50 for part in parts)
    assert len(parts) == math.ceil(n / 50)
    if n == 3121:
        assert len(parts) == 63


@given(n=st.integers(0, 3300))
def test_batches_of_50(n: int) -> None:
    """Every batch but the last is exactly 50 symbols."""
    parts = batches(list(range(n)))
    assert all(len(part) == 50 for part in parts[:-1])


def quote_for(symbol: str, day: date) -> dict:
    return {"symbol": symbol, "close": 10.0, "date": day.isoformat()}


@given(
    symbols=symbols_list,
    trade_date=st.dates(min_value=date(2011, 1, 1), max_value=date(2035, 1, 1)),
    data=st.data(),
)
def test_date_mismatch_rejected(symbols: list[str], trade_date: date, data: st.DataObject) -> None:
    """For every response, a row is kept only if its returned date is the trade date; every
    other row (another date, a missing or unreadable date) is in the mismatch report, and every
    requested symbol is a kept row, a mismatch or reported missing."""
    offsets = {
        s: data.draw(st.one_of(st.just(0), st.integers(-400, 400), st.none()), label=s)
        for s in symbols
    }
    bad_dates = {
        s: data.draw(st.sampled_from(["", "garbage", None]), label=f"bad {s}") for s in symbols
    }
    dropped = {s for s in symbols if data.draw(st.booleans(), label=f"drop {s}")}

    def respond(_tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        quotes = []
        for symbol in arguments["symbols"]:
            if symbol in dropped:
                continue
            offset = offsets[symbol]
            quote = quote_for(symbol, trade_date + timedelta(days=offset or 0))
            if offset is None:
                quote["date"] = bad_dates[symbol]
            quotes.append(quote)
        return {"count": len(quotes), "quotes": quotes}

    client = McpClient(ScriptedTransport(respond), sleep=lambda _s: None)
    result = client.bulk_quotes(symbols, trade_date)
    kept = {r["symbol"] for r in result.rows}
    mismatched = {r["symbol"] for r in result.mismatched}
    assert all(r["date"] == trade_date.isoformat() for r in result.rows)
    assert kept == {s for s in symbols if s not in dropped and offsets[s] == 0}
    assert mismatched == {s for s in symbols if s not in dropped and offsets[s] != 0}
    assert set(result.missing) == dropped
    assert kept | mismatched | set(result.missing) == set(symbols)


@settings(max_examples=40)
@given(
    n=st.integers(1, 400),
    limit=st.integers(1, 6),
    hold=st.sampled_from([0.0, 0.0005]),
)
def test_concurrency_bounded(n: int, limit: int, hold: float) -> None:
    """For every workload and every interleaving of completions, the calls in flight seen by
    the transport never exceed the configured limit (default 4)."""
    symbols = [f"S{i}" for i in range(n)]
    day = date(2026, 10, 19)
    transport = ScriptedTransport(
        lambda _t, args: {"quotes": [quote_for(s, day) for s in args["symbols"]]}, hold=hold
    )
    client = McpClient(transport, max_concurrency=limit, sleep=lambda _s: None)
    result = client.bulk_quotes(symbols, day)
    assert transport.max_in_flight <= limit
    assert len(transport.calls) == math.ceil(n / 50)
    assert len(result.rows) == n


def test_default_concurrency_is_four() -> None:
    transport = ScriptedTransport(lambda *_a: {"quotes": []}, hold=0.001)
    client = McpClient(transport, sleep=lambda _s: None)
    client.map_calls(
        [lambda: client.call("nse-bhavcopy", "get_bulk_quote", {"symbols": []}) for _ in range(40)]
    )
    assert transport.max_in_flight <= 4


# --- the nightly ingest -------------------------------------

MINUTES = 60
CUTOFF_MINUTE = 22 * MINUTES
SERIES_POOL = ["EQ", "BE", "BZ", "SM", "GS"]
EQ_BE = ("EQ", "BE")
DAY_BEFORE = TRADE_DAY - timedelta(days=1)
RELIANCE_PROBE = ["RELIANCE"]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bulk_symbols(world: World) -> list[str]:
    """The symbols of every full-fetch call the server saw (the one-symbol probe left out)."""
    return [
        s
        for args in world.nse.tools("get_bulk_quote")
        if args["symbols"] != RELIANCE_PROBE
        for s in args["symbols"]
    ]


@given(
    start=st.integers(0, 24 * MINUTES - 1),
    ready=st.one_of(st.none(), st.integers(17 * MINUTES, 23 * MINUTES)),
)
@example(start=0, ready=None)
@example(start=18 * MINUTES, ready=18 * MINUTES + 5)
@example(start=22 * MINUTES, ready=None)
def test_probe_schedule(start: int, ready: int | None) -> None:
    """For every start time and every moment the server begins answering with the trade date:
    probes are made from the later of the start and 18:00, every 10 minutes, none at or after
    22:00, and they stop at the first one that matched; the run is ready exactly when a probe
    matched, and otherwise 22:00 is what ended it (so the fallback, and only the fallback,
    follows)."""
    clock = FixedClock(day_at(0) + timedelta(minutes=start))
    ready_at = None if ready is None else day_at(0) + timedelta(minutes=ready)

    def probe() -> date:
        matched = ready_at is not None and clock.now() >= ready_at
        return TRADE_DAY if matched else DAY_BEFORE

    outcome = wait_for_data(probe, TRADE_DAY, clock, sleeper(clock))
    minutes = [int((t - day_at(0)).total_seconds() // 60) for t in outcome.probes]
    scheduled = list(range(max(start, 18 * MINUTES), CUTOFF_MINUTE, 10))
    hit = None if ready is None else next((m for m in scheduled if m >= ready), None)
    assert minutes == (scheduled if hit is None else scheduled[: scheduled.index(hit) + 1])
    assert outcome.ready == (hit is not None)
    assert all(18 * MINUTES <= m < CUTOFF_MINUTE for m in minutes)
    assert all(b - a >= 10 for a, b in zip(minutes, minutes[1:], strict=False))


@settings(max_examples=25)
@given(spec=st.lists(st.tuples(st.sampled_from(SERIES_POOL), st.booleans()), max_size=130))
def test_fetch_covers_universe(spec: list[tuple[str, bool]]) -> None:
    """For every instrument master, the symbols the fetch requests are exactly the active
    instruments with series EQ or BE, each once; an empty universe fails the run before any
    fetch."""
    universe = [(f"S{i:04d}", series, active) for i, (series, active) in enumerate(spec)]
    wanted = {s for s, series, active in universe if active and series in EQ_BE}
    with tempfile.TemporaryDirectory() as tmp:
        world = make_world(tmp, universe)
        try:
            run_ingest(world.deps(), TRADE_DAY, uuid7())
        except JobError as exc:
            assert not wanted
            assert exc.code == CODE_NO_UNIVERSE
            assert world.nse.calls == []
            return
        requested = _bulk_symbols(world)
    assert wanted
    assert sorted(requested) == sorted(wanted)


NUMBERS = st.integers(-1, 6)


@settings(max_examples=300)
@example(o=1, h=5, low=2, c=3, prev=None, volume=0, day=TRADE_DAY.isoformat())
@example(o=3, h=5, low=2, c=3, prev=None, volume=0, day=TRADE_DAY.isoformat())
@example(o=3, h=5, low=2, c=1, prev=None, volume=0, day=TRADE_DAY.isoformat())
@example(o=3, h=5, low=2, c=3, prev=0, volume=0, day=TRADE_DAY.isoformat())
@given(
    o=NUMBERS,
    h=NUMBERS,
    low=NUMBERS,
    c=NUMBERS,
    prev=st.one_of(st.none(), NUMBERS),
    volume=st.one_of(st.integers(-3, 3), st.just(1.5), st.none(), st.just("x")),
    day=st.sampled_from([TRADE_DAY.isoformat(), DAY_BEFORE.isoformat(), "junk", None]),
)
def test_row_checks(
    o: int, h: int, low: int, c: int, prev: int | None, volume: object, day: str | None
) -> None:
    """A row is accepted if and only if its date is the trade date, every price is above zero,
    low <= open <= high, low <= close <= high, prev_close is null or above zero and volume is a
    whole number of at least zero."""
    raw = {
        "open": float(o),
        "high": float(h),
        "low": float(low),
        "close": float(c),
        "prev_close": None if prev is None else float(prev),
        "volume": volume,
        "date": day,
    }
    accepted = check_quote(raw, TRADE_DAY) is not None
    expected = (
        day == TRADE_DAY.isoformat()
        and 0 < low <= o <= h
        and low <= c <= h
        and (prev is None or prev > 0)
        and isinstance(volume, int)
        and volume >= 0
    )
    assert accepted == expected


@settings(max_examples=20)
@given(
    attempts=st.lists(
        st.tuples(st.sampled_from(["mcp", "fallback"]), st.integers(1, 50), st.booleans()),
        min_size=1,
        max_size=4,
    )
)
def test_raw_write_once(attempts: list[tuple[str, int, bool]]) -> None:
    """For every sequence of ingest attempts for one trade date (either source, each with
    different prices, some failing after the write), exactly one raw partition exists after
    the first attempt, its bytes never change afterwards, and the fallback ran at most once."""
    names = [s for s, _, _ in symbols_of(6)]
    with tempfile.TemporaryDirectory() as tmp:
        world = make_world(tmp, symbols_of(6))
        first: tuple[Path, str, Decimal] | None = None
        for mode, price, index_ok in attempts:
            world.nse.default = Quote(Decimal(price), Decimal(price))
            world.nse.index_rows = list(INDEX_ROWS) if index_ok else []
            if mode == "fallback":
                world.nse.ready_at = None
                world.clock.set(day_at(22, 0))
                world.fallback_day = bhav_day(world, dict.fromkeys(names, Decimal(price)))
            else:
                world.nse.ready_at = day_at(18, 0)
                world.clock.set(day_at(18, 0))
            try:
                run_ingest(world.deps(), TRADE_DAY, uuid7())
            except JobError:
                pass
            files = world.store.partition_files(DATASET_BARS, TRADE_DAY)
            assert len(files) == 1
            if first is None:
                first = (files[0], _sha(files[0]), Decimal(price))
            assert first[:2] == (files[0], _sha(files[0]))
        assert first is not None
        closes = set(world.store.read_bars(TRADE_DAY)["close"].to_list())
        fallbacks = len(world.fallback_calls)
    assert closes == {first[2]}
    assert fallbacks <= 1


def _killed_after_write(store: object) -> None:
    """The process is killed right after the day's raw partition is written."""
    write = store.write_bars  # type: ignore[attr-defined]

    def write_then_die(*args: object, **kwargs: object) -> object:
        write(*args, **kwargs)
        raise Crash

    store.write_bars = write_then_die  # type: ignore[attr-defined]


@settings(max_examples=20)
@given(
    first=st.tuples(st.sets(st.integers(0, 4), max_size=4), st.booleans()),
    retries=st.lists(st.sets(st.integers(0, 4), max_size=4), min_size=1, max_size=3),
)
def test_checks_kept_for_retry(first: tuple[set[int], bool], retries: list[set[int]]) -> None:
    """For every first ingest attempt that writes the raw partition and then fails (no index
    rows) or is killed right after the write, every retry, dropping other symbols, reports the
    row-check counts of the first attempt: not its own and not 0."""
    names = [s for s, _, _ in symbols_of(6)]
    dropped, killed = first
    with tempfile.TemporaryDirectory() as tmp:
        world = make_world(tmp, symbols_of(6))
        world.nse.drop = {names[i] for i in dropped}
        world.nse.index_rows = []
        if killed:
            _killed_after_write(world.store)
        try:
            run_ingest(world.deps(), TRADE_DAY, uuid7())
        except (JobError, Crash):
            pass
        vars(world.store).pop("write_bars", None)
        world.nse.index_rows = list(INDEX_ROWS)
        reported = []
        for again in retries:
            world.nse.drop = {names[i] for i in again}
            reported.append(run_ingest(world.deps(), TRADE_DAY, uuid7()).missing)
    assert reported == [len(dropped)] * len(retries)


class _Attempt:
    """What one scenario does to the world and where it leaves a job run."""

    def __init__(self, final: str, fallback: bool) -> None:
        self.final = final
        self.fallback = fallback


SCENARIOS = {
    "success": _Attempt("succeeded", False),
    "mcp_failure": _Attempt("failed", False),
    "fallback_success": _Attempt("succeeded", True),
    "fallback_failure": _Attempt("failed", True),
    "crash": _Attempt("running", False),
}


def _arrange(world: World, scenario: str) -> None:
    nse = world.nse
    nse.fail_bulk = scenario == "mcp_failure"
    nse.crash = scenario == "crash"
    world.fallback_day = None
    if SCENARIOS[scenario].fallback:
        nse.ready_at = None
        world.clock.set(day_at(22, 0))
        if scenario == "fallback_success":
            world.fallback_day = bhav_day(world, {"S0000": Decimal(100), "S0001": Decimal(100)})
    else:
        nse.ready_at = day_at(18, 0)
        world.clock.set(day_at(18, 0))


@settings(max_examples=25)
@given(plan=st.lists(st.sampled_from(sorted(SCENARIOS)), min_size=1, max_size=4))
def test_outcome_event(plan: list[str]) -> None:
    """For every sequence of runs of one night (success, MCP failure, fallback success,
    fallback failure, a crash before the commit), each run that reaches a final status leaves
    exactly one `ingest.completed` or `ingest.failed` event matching that status, a run that
    does not (a crash, or a call after success or a crash) leaves none, and the data-health kill
    is tripped if and only if some run failed."""
    with tempfile.TemporaryDirectory() as tmp:
        world = make_world(tmp, symbols_of(2))
        state, finals, any_failed, fallbacks = "none", [], False, 0
        for scenario in plan:
            _arrange(world, scenario)
            holder: dict = {}
            runs = state in ("none", "failed")
            try:
                run_job(
                    "ingest.nightly",
                    TRADE_DAY,
                    ingest_body(world.deps(), TRADE_DAY, holder),
                    session_factory=world.factory,
                    on_finish=outcome_recorder(holder, TRADE_DAY),
                )
            except Crash:
                pass
            if runs:
                state = SCENARIOS[scenario].final
                fallbacks += SCENARIOS[scenario].fallback
                if state != "running":
                    finals.append(state)
                any_failed = any_failed or state == "failed"
        with world.factory() as session:
            row = session.scalars(select(HatJobRun)).one()
            events = session.scalars(
                select(HatEvent.event_type)
                .where(HatEvent.aggregate_id == row.id)
                .order_by(HatEvent.id)
            ).all()
            tripped = current_state(kill_rows(session), "account", None, "health") == TRIPPED
        assert row.status == state
        assert [e.split(".")[1] for e in events] == [
            "completed" if f == "succeeded" else "failed" for f in finals
        ]
        assert tripped == any_failed
        assert len(world.fallback_calls) == fallbacks


@settings(max_examples=20)
@given(n=st.integers(1, 160), data=st.data())
def test_call_budget(n: int, data: st.DataObject) -> None:
    """For every universe of N symbols with F flagged (and no retries), a run makes at most
    ceil(N / 50) bulk calls + its probes + one index call + one breadth call + F lookups."""
    flagged = data.draw(st.sets(st.integers(0, n - 1), max_size=min(n, 12)))
    names = [s for s, _, _ in symbols_of(n)]
    with tempfile.TemporaryDirectory() as tmp:
        world = make_world(tmp, symbols_of(n))
        store_previous(
            world,
            {s: Decimal(90 if i in flagged else 100) for i, s in enumerate(names)},
            DAY_BEFORE,
        )
        deps = world.deps()
        report = run_ingest(deps, TRADE_DAY, uuid7())
        calls = deps.client.log.calls
    assert report.flagged == len(flagged)
    assert calls <= math.ceil(n / 50) + report.probes + 2 + len(flagged)


INDEX_VARIANTS = ["ok", "no_vix", "no_nifty500", "empty", "wrong_date", "error", "repeated"]
BREADTH_VARIANTS = ["ok", "error", "wrong_date", "partial"]


@settings(max_examples=30)
@given(index=st.sampled_from(INDEX_VARIANTS), breadth=st.sampled_from(BREADTH_VARIANTS))
def test_nightly_index_breadth(index: str, breadth: str) -> None:
    """For every answer of the index and breadth calls, the run succeeds if and only if the
    index rows of the trade date, with NIFTY 500 and India VIX among them, are stored, one row
    per returned index; on success the day's one breadth row exists (the server's when it
    answered for the trade date, every field of its recorded snake_case reply kept, counted from
    our bars otherwise)."""
    rows = list(INDEX_ROWS)
    if index == "no_vix":
        rows = rows[:2]
    elif index == "no_nifty500":
        rows = [rows[0], rows[2]]
    elif index == "empty":
        rows = []
    elif index == "repeated":
        rows = [*rows, *rows]
    with tempfile.TemporaryDirectory() as tmp:
        world = make_world(tmp, symbols_of(4))
        nse = world.nse
        nse.index_rows = None if index == "error" else rows
        nse.index_date = DAY_BEFORE if index == "wrong_date" else TRADE_DAY
        if breadth == "error":
            nse.breadth = None
        elif breadth == "wrong_date":
            nse.breadth = {**(nse.breadth or {}), "date": DAY_BEFORE.isoformat()}
        elif breadth == "partial":
            nse.breadth = {"date": TRADE_DAY.isoformat(), "advances": 3}
        try:
            report = run_ingest(world.deps(), TRADE_DAY, uuid7())
        except JobError:
            assert index in ("no_vix", "no_nifty500", "empty", "wrong_date", "error")
            return
        stored = {r.index_name for r in world.store.read_index(TRADE_DAY)}
        market = world.store.read_partition(DATASET_MARKET_BREADTH, TRADE_DAY)
    assert index in ("ok", "repeated")
    assert stored == {"NIFTY 50", "NIFTY 500", "INDIA VIX"}
    assert report.index_rows == 3
    assert market.height == 1
    assert market["source"][0] == ("mcp" if breadth == "ok" else "derived")
    if breadth == "ok":  # the live server's snake_case reply is stored as it was sent
        sent = nse.breadth or {}
        assert market["volume"][0] == sent["total_volume"]
        assert market["total_stocks"][0] == sent["total_stocks"]
        assert market["ad_ratio"][0] == sent["ad_ratio"]
        for count in ("advances", "declines", "unchanged"):
            assert market[count][0] == sent[count]


@settings(max_examples=20)
@given(
    shape=st.lists(st.lists(st.integers(1, 4), min_size=1, max_size=3), min_size=1, max_size=3),
)
def test_renames_from_nse_data(shape: list[list[int]]) -> None:
    """For every history in which each (ISIN, series) trades under a run of symbols, seeding
    the renames derived from the stored bars makes every (symbol, day) of the bars resolve to
    the instrument that traded it, leaves each instrument's symbol intervals disjoint, and
    seeding again changes nothing."""
    from hat_mcp_support import security

    from app.hat.data.instruments import upsert_instruments
    from app.hat.models import HatInstrumentSymbolHistory

    days = weekdays(date(2026, 3, 2), date(2026, 3, 27))
    tracks = []  # per ISIN: the symbol held on each day
    for k, lengths in enumerate(shape):
        symbols = [f"K{k}S{j}" for j, length in enumerate(lengths) for _ in range(length)]
        tracks.append(symbols[: len(days)])
    engine = reference_engine()
    with tempfile.TemporaryDirectory() as tmp, Session(engine) as session:
        store = DataStore(Path(tmp))
        market_id = seed_market(session)
        upsert_instruments(
            session,
            market_id,
            [security(f"INE{k:09d}", "EQ", t[-1]) for k, t in enumerate(tracks)],
            today=days[0],
        )
        ids = {
            r.isin: r.id
            for r in session.scalars(select(HatInstrument).order_by(HatInstrument.isin))
        }
        run = uuid7()
        for d, day in enumerate(days):
            rows = [
                BarRow(
                    ids[f"INE{k:09d}"],
                    f"INE{k:09d}",
                    t[d],
                    "EQ",
                    *(Decimal(10),) * 5,
                    1,
                    None,
                    "bhavcopy",
                    run,
                )
                for k, t in enumerate(tracks)
                if d < len(t)
            ]
            if rows:
                store.write_bars(day, rows)
        seed_symbol_history(session, market_id, renames_from_bars(store))
        session.flush()

        def intervals() -> set[tuple]:
            return {
                (h.instrument_id, h.symbol, h.valid_from, h.valid_to)
                for h in session.scalars(select(HatInstrumentSymbolHistory))
            }

        once = intervals()
        seed_symbol_history(session, market_id, renames_from_bars(store))
        session.flush()
        assert intervals() == once
        for k, t in enumerate(tracks):
            for d, symbol in enumerate(t):
                found = resolve_symbol(session, market_id, symbol, days[d])
                assert found is not None, (symbol, days[d])
                assert found.id == ids[f"INE{k:09d}"], (symbol, days[d])
            spans = sorted(
                (h.valid_from, h.valid_to)
                for h in session.scalars(
                    select(HatInstrumentSymbolHistory).where(
                        HatInstrumentSymbolHistory.instrument_id == ids[f"INE{k:09d}"]
                    )
                )
            )
            for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
                assert end is not None
                assert end <= start
