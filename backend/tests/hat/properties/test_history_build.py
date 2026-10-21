"""PBT history_build.* (HAT-DATA-091, 092, 093, 096; HAT-DEC-I2): the whole build runs offline on
generated bhavcopy zips in both formats across the 8 Jul 2024 switch. Completeness, no duplicate
(instrument, date), a missing day reported and left empty, and a repeated or resumed run changing
nothing."""

import hashlib
import tempfile
from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hat_bhav_support import prices, render_old, render_udiff
from hat_mcp_support import ScriptedTransport
from hypothesis import given, settings
from hypothesis import strategies as st

from app.hat.data.bhavcopy import BhavRow, FetchResult, archive_url, weekdays
from app.hat.data.history_build import (
    DictCatalog,
    Pacing,
    history_build_job,
    mcp_close_source,
    mcp_index_source,
    run_history_build,
)
from app.hat.data.mcp_client import McpClient, McpError
from app.hat.data.store import DATASET_BARS, DataStore
from app.hat.ops.jobs import JobError

WINDOW = weekdays(date(2024, 6, 24), date(2024, 7, 19))
POOL = [(f"INE00000000{i}", "EQ" if i < 4 else "BE") for i in range(6)]


@st.composite
def eras(draw: st.DrawFn) -> dict[date, list[BhavRow] | None]:
    """For each weekday: the rows NSE has for it, or None when there is no file."""
    out: dict[date, list[BhavRow] | None] = {}
    for day in WINDOW:
        if draw(st.integers(0, 4)) == 0:
            out[day] = None
            continue
        chosen = draw(st.lists(st.sampled_from(POOL), min_size=1, max_size=4, unique=True))
        out[day] = [
            BhavRow(
                day,
                isin,
                series,
                f"S{isin[-1]}",
                draw(prices),
                draw(prices),
                draw(prices),
                draw(prices),
                draw(prices),
                draw(st.integers(0, 10**7)),
                draw(st.decimals("0", "99999999", places=2)),
            )
            for isin, series in chosen
        ]
    return out


def _fetcher(world: dict[date, list[BhavRow] | None], requests: list[date]):  # noqa: ANN202
    by_url = {archive_url(d): d for d in world}

    def fetch(url: str) -> FetchResult:
        day = by_url[url]
        requests.append(day)
        rows = world[day]
        if rows is None:
            return FetchResult("not_found")
        blob = render_old(day, rows, 1) if day <= date(2024, 7, 5) else render_udiff(day, rows, 1)
        return FetchResult("ok", blob)

    return fetch


def _files(root: Path) -> dict[str, str]:
    """Path to content digest: a failing comparison must print short hashes, not Parquet bytes
    (pytest's diff of large byte strings exhausts memory)."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@given(world=eras(), holiday_pick=st.sets(st.sampled_from(WINDOW)))
@settings(max_examples=40)
def test_build_offline_end_to_end(
    world: dict[date, list[BhavRow] | None], holiday_pick: set[date]
) -> None:
    """The offline build writes exactly the rows each file holds (same normalised rows on both
    sides of 8 Jul 2024), one per (instrument, date); a day with no file stays empty and is
    reported as an explained holiday or a gap; a resumed run requests nothing and a full rerun
    rewrites identical data."""
    missing = {d for d, rows in world.items() if rows is None}
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        catalog = DictCatalog(auto=True)
        asked: list[date] = []
        args = {
            "store": store,
            "catalog": catalog,
            "start": WINDOW[0],
            "end": WINDOW[-1],
            "holidays": holiday_pick,
            "pacing": Pacing(interval=0),
        }
        report = run_history_build(fetch=_fetcher(world, asked), resume=True, **args)  # type: ignore[arg-type]
        assert sorted(asked) == WINDOW
        assert len(asked) == len(set(asked))
        assert report.unresolved == set()
        assert report.problems == {}
        assert set(report.days.holidays) == missing & holiday_pick
        assert set(report.days.gaps) == missing - holiday_pick
        assert report.days.unaccounted == []
        assert set(report.partitions.missing) == missing - holiday_pick  # only gaps are missing
        con = store.connect()
        got = con.execute(
            "SELECT date, isin, series, symbol, open, high, low, close, prev_close, volume, "
            "traded_value FROM bars_daily"
        ).fetchall()
        keys = con.execute(
            "SELECT instrument_id, date, count(*) FROM bars_daily_raw GROUP BY ALL"
        ).fetchall()
        con.close()
        assert all(n == 1 for *_, n in keys)
        expected = [
            (
                d,
                r.isin,
                r.series,
                r.symbol,
                r.open,
                r.high,
                r.low,
                r.close,
                r.prev_close,
                r.volume,
                r.traded_value,
            )
            for d, rows in world.items()
            for r in rows or []
        ]
        assert sorted(got) == sorted(expected)
        for day in missing:
            assert store.read_bars(day).height == 0  # never filled
        assert sorted(store.days(DATASET_BARS)) == sorted(set(WINDOW) - missing)
        snapshot = _files(store.root)

        again: list[date] = []
        run_history_build(fetch=_fetcher(world, again), resume=True, **args)  # type: ignore[arg-type]
        assert again == []
        assert _files(store.root) == snapshot

        full: list[date] = []
        run_history_build(fetch=_fetcher(world, full), resume=False, **args)  # type: ignore[arg-type]
        assert sorted(full) == sorted(missing)  # only the days with no file are asked again
        assert _files(store.root) == snapshot

        body = history_build_job(fetch=_fetcher(world, []), resume=True, **args)
        if missing - holiday_pick or any(r == [] for r in world.values()):
            with pytest.raises(JobError):
                body()  # gaps and empty files are failures (checked in full below)
        else:
            first = body()
            assert body() == first
        assert _files(store.root) == snapshot


@given(
    world=eras(),
    holiday_pick=st.sets(st.sampled_from(WINDOW)),
    error_pick=st.sets(st.sampled_from(WINDOW), max_size=2),
)
@settings(max_examples=40)
def test_job_fails_on_any_gap_or_failed_download(
    world: dict[date, list[BhavRow] | None], holiday_pick: set[date], error_pick: set[date]
) -> None:
    """The job body succeeds only when every weekday is either stored or an explained holiday:
    a failed download (left unaccounted), a missing file that is no holiday, or a file with no
    usable rows raises `history_incomplete`."""
    by_url = {archive_url(d): d for d in world}
    base = _fetcher(world, [])

    def fetch(url: str) -> FetchResult:
        return FetchResult("error") if by_url[url] in error_pick else base(url)

    missing = {d for d, rows in world.items() if rows is None} - error_pick
    empty = {d for d, rows in world.items() if rows == [] and d not in error_pick}
    must_fail = bool(error_pick or empty or missing - holiday_pick)
    with tempfile.TemporaryDirectory() as tmp:
        body = history_build_job(
            store=DataStore(Path(tmp)),
            fetch=fetch,
            catalog=DictCatalog(auto=True),
            start=WINDOW[0],
            end=WINDOW[-1],
            holidays=holiday_pick,
            pacing=Pacing(interval=0),
        )
        if must_fail:
            with pytest.raises(JobError):
                body()
        else:
            assert body().rows >= 0


def test_build_offline_unresolved_security_is_reported() -> None:
    """A security the catalog cannot place is reported and its row is not written."""
    day = date(2024, 7, 8)
    row = BhavRow(day, "INE000000001", "EQ", "AAA", *(Decimal("10"),) * 5, 100, Decimal("1000"))
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        report = run_history_build(
            store=store,
            fetch=lambda url: FetchResult("ok", render_udiff(day, [row])),
            catalog=DictCatalog(),
            start=day,
            end=day,
            pacing=Pacing(interval=0),
        )
        assert report.unresolved == {("INE000000001", "EQ")}
        assert report.rows_written == 0
        assert store.read_bars(day).height == 0


# --- the MCP history wrappers (HAT-DATA-094, 095, 096) ---------------------------------------

SERIES_DAYS = weekdays(date(2021, 1, 1), date(2023, 6, 30))


def _index_server(values: dict[date, Decimal]) -> ScriptedTransport:
    """A fake `get_index_history`: chunks of `months` x 30 days ending at endDate, newest first,
    with the `next_end_date` the real server gives."""

    def answer(tool: str, args: Mapping[str, Any]) -> object:
        end = date.fromisoformat(args["endDate"])
        begin = end - timedelta(days=30 * int(args["months"]) - 1)
        data = [
            {
                "indexName": args["indexName"],
                "open": str(values[d]),
                "high": str(values[d]),
                "low": str(values[d]),
                "close": str(values[d]),
                "date": d.isoformat(),
            }
            for d in sorted(values)
            if begin <= d <= end
        ]
        return {"data": data, "next_end_date": (begin - timedelta(days=1)).isoformat()}

    return ScriptedTransport(answer)


@given(
    present=st.sets(st.sampled_from(SERIES_DAYS), min_size=1, max_size=120),
    start=st.sampled_from(SERIES_DAYS),
    span=st.integers(0, 400),
)
@settings(max_examples=40)
def test_mcp_index_source_chains(present: set[date], start: date, span: int) -> None:
    """Chaining `get_index_history` chunks back from the end yields every present day in
    [start, end] exactly once and nothing outside it."""
    end = start + timedelta(days=span)
    values = {d: Decimal(i + 1) for i, d in enumerate(sorted(present))}
    client = McpClient(_index_server(values), sleep=lambda _s: None)
    got = list(mcp_index_source(client)("India VIX", start, end))
    assert sorted(p.day for p in got) == sorted(d for d in present if start <= d <= end)
    assert all(p.row.close == values[p.day] and p.row.index_name == "INDIA VIX" for p in got)


@given(
    close=st.decimals("1", "9999", places=2),
    other=st.sampled_from(["no_row", "other_day", "bad_close", "ok"]),
)
def test_mcp_close_source_exact(close: Decimal, other: str) -> None:
    """The MCP close for a symbol-day is the row dated that day, exactly; another day's row, a
    missing row or an unreadable close is None (unchecked), never a neighbour's value."""
    day = date(2026, 10, 14)
    rows = {
        "ok": [{"date": day.isoformat(), "close": float(close)}],
        "other_day": [{"date": "2026-10-13", "close": float(close)}],
        "bad_close": [{"date": day.isoformat(), "close": "n/a"}],
        "no_row": [],
    }[other]
    client = McpClient(ScriptedTransport([{"data": rows}]), sleep=lambda _s: None)
    got = mcp_close_source(client)("RELIANCE", day)
    assert got == (close if other == "ok" else None)


@given(has_warning=st.booleans(), error=st.booleans())
def test_mcp_corporate_actions_page(has_warning: bool, error: bool) -> None:
    """An `error` answer is a failed lookup (raised), never an empty list of actions; a
    `warning` marks the page incomplete."""
    payload: dict[str, Any] = {"actions": []}
    if has_warning:
        payload["warning"] = "partial"
    if error:
        payload = {"error": "lookup failed"}
    client = McpClient(ScriptedTransport([payload]), sleep=lambda _s: None)
    start, end = date(2018, 1, 1), date(2018, 12, 31)
    if error:
        with pytest.raises(McpError):
            client.corporate_actions("INFY", start, end)
    else:
        page = client.corporate_actions("INFY", start, end)
        assert page.actions == []
        assert page.incomplete is has_warning
