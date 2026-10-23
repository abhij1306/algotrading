"""PBT ingest.* (..045, 048, 049; ): the MCP client's batching, concurrency and date check, the
nightly ingest (probe,
universe, row checks, raw write-once, outcome event, call budget, index and breadth), symbol
renames from the stored bars, and the Parquet store both sources write.

`ingest.event_and_state_atomic` is a check: an ingest outcome and its event are written by
one transaction; the nightly ingest's own hook is checked by `ingest.outcome_event`."""

import dataclasses
import math
import tempfile
import uuid
from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from hat_ingest_support import (
    TRADE_DAY,
    day_at,
    sleeper,
)
from hat_mcp_support import ScriptedTransport
from hat_support import event_engine
from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.hat.clock import FixedClock
from app.hat.data.ingest import (
    check_quote,
    wait_for_data,
)
from app.hat.data.mcp_client import McpClient, batches
from app.hat.data.rowcheck import Candidate, row_ok
from app.hat.data.store import (
    BAR_SCHEMA,
    BarRow,
    DataStore,
)
from app.hat.ids import uuid7
from app.hat.models import HatJobRun

ENGINE = event_engine(HatJobRun)

outcomes = st.sampled_from(["ingest.completed", "ingest.failed", "quality.failed"])
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
DAY_BEFORE = TRADE_DAY - timedelta(days=1)


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


def test_row_ok_refuses_a_price_the_store_cannot_hold() -> None:
    """decimal(18, 4): 1e14 or a fifth decimal place is refused; trailing zeros are fine."""
    day = date(2024, 1, 1)

    def ok(high: str) -> bool:
        return row_ok(
            Candidate(day, Decimal(10), Decimal(high), Decimal(9), Decimal(11), Decimal(10), 5), day
        )

    assert ok("12.00000")
    assert not ok("100000000000000")
    assert not ok("12.00001")
