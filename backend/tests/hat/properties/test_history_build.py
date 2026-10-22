"""PBT history_build.* (HAT-DATA-091, 092, 093, 096; HAT-DEC-I2): the whole build runs offline on
generated bhavcopy zips in both formats across the 8 Jul 2024 switch. Completeness, no duplicate
(instrument, date), a missing day reported and left empty, and a repeated or resumed run changing
nothing."""

import hashlib
import io
import itertools
import tempfile
from collections.abc import Iterator, Mapping
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from hat_bhav_support import prices, render_old, render_udiff
from hat_mcp_support import ScriptedTransport, seed_market
from hat_support import ROOT_SOURCES, memory_engine, no_network, operator_env
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import sessionmaker

from app.hat.data import history_build
from app.hat.data.bhavcopy import (
    BhavRow,
    DayReconciliation,
    DownloadReport,
    Fetcher,
    FetchResult,
    archive_url,
    weekdays,
)
from app.hat.data.corporate_actions import LoadReport
from app.hat.data.history_build import (
    INDEX_NAMES,
    ArchiveIndexSource,
    BuildReport,
    DictCatalog,
    IndexPoint,
    Pacing,
    Validation,
    archive_fill_source,
    history_build_job,
    index_archive_name,
    load_index_history,
    mcp_close_source,
    mcp_index_source,
    run_history_build,
)
from app.hat.data.mcp_client import CODE_BAD_RESPONSE, McpClient, McpError
from app.hat.data.store import (
    DATASET_BARS,
    BarRow,
    DataStore,
    IndexRow,
    PartitionReport,
    index_coverage,
)
from app.hat.models import HatMarket, HatTradingCalendar
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


CHUNK_DAYS = 30 * 12  # the fake server's chunk for MCP_HISTORY_MONTHS


@given(
    present=st.sets(st.sampled_from(SERIES_DAYS), min_size=1, max_size=120),
    depth=st.sampled_from(SERIES_DAYS),
    start=st.sampled_from(SERIES_DAYS),
    span=st.integers(0, 900),
    other_error=st.booleans(),
)
@settings(max_examples=40)
def test_mcp_index_source_ends_at_depth(
    present: set[date], depth: date, start: date, span: int, other_error: bool
) -> None:
    """PR 44: a server that keeps history only from `depth` answers an older window with its
    "No data for index" error. The chain then ends: it yields every present day from `depth` in
    [start, end] and asks nothing more. Any other `error` answer is a failure (raised as
    `mcp_bad_response`), never the end of the history."""
    end = start + timedelta(days=span)
    values = {d: Decimal(i + 1) for i, d in enumerate(sorted(present)) if d >= depth}
    served = _index_server(values)

    def answer(tool: str, args: Mapping[str, Any]) -> object:
        if date.fromisoformat(args["endDate"]) < depth:
            if other_error:
                return {"error": "upstream unavailable"}
            return {"error": f"No data for index '{args['indexName']}' between x and y"}
        return served.call("nse-bhavcopy", tool, args)

    transport = ScriptedTransport(answer)
    source = mcp_index_source(McpClient(transport, sleep=lambda _s: None))
    cursors = [end - timedelta(days=CHUNK_DAYS * k) for k in range((end - start).days // 360 + 1)]
    reaches = any(c < depth for c in cursors if c >= start)
    if other_error and reaches:
        with pytest.raises(McpError) as caught:
            list(source("NIFTY 50", start, end))
        assert caught.value.code == CODE_BAD_RESPONSE
        return
    got = list(source("NIFTY 50", start, end))
    assert sorted(p.day for p in got) == sorted(d for d in values if start <= d <= end)
    asked = [date.fromisoformat(args["endDate"]) for _s, _t, args in transport.calls]
    assert sum(d < depth for d in asked) == (1 if reaches else 0)
    assert all(d < depth for d in asked[-1:]) == reaches  # the no-data answer was the last call


FILL_DAYS = weekdays(date(2023, 9, 1), date(2023, 11, 30))
OLD_NAMES = {"NIFTY 50": "CNX Nifty", "NIFTY 500": "CNX 500"}  # the archive's names before 2015


def _archive_csv(day: date, old_names: bool) -> bytes:
    """An `ind_close_all` file in NSE's layout [A]; values tagged .5 so archive rows are known."""
    lines = [
        "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
        "Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield"
    ]
    for name in (*INDEX_NAMES, "NIFTY BANK"):
        shown = OLD_NAMES.get(name, name.title()) if old_names else name.title()
        v = f"{day.toordinal()}.5"
        lines.append(f'"{shown}",{day:%d-%m-%Y},{v},{v},{v},{v},1.0,0.1,100,"1,234.5",20,3,1')
    return ("\n".join(lines) + "\n").encode()


def _bars(store: DataStore, days: set[date]) -> None:
    one = Decimal(1)
    for day in days:
        store.write_bars(
            day,
            [
                BarRow(
                    "i1",
                    "INE000000001",
                    "S1",
                    "EQ",
                    one,
                    one,
                    one,
                    one,
                    None,
                    1,
                    None,
                    "bhavcopy",
                    "r",
                )
            ],
        )


@given(
    bars=st.sets(st.sampled_from(FILL_DAYS), min_size=1),
    depths=st.fixed_dictionaries(
        {n: st.one_of(st.none(), st.sampled_from(FILL_DAYS)) for n in INDEX_NAMES}
    ),
    files=st.dictionaries(
        st.sampled_from(FILL_DAYS), st.sampled_from(["ok", "old_names", "not_found", "error"])
    ),
    usual=st.sampled_from(["ok", "old_names"]),
)
@settings(max_examples=40, deadline=None)
def test_archive_fills_before_mcp_depth(
    bars: set[date], depths: dict[str, date | None], files: dict[date, str], usual: str
) -> None:
    """PR 44: with the MCP serving each index only from its depth (or not at all), the daily
    `ind_close_all` archive fills every earlier day that has bars, under today's index names
    whatever the file calls them, and the MCP's rows win from its depth on. Only a day whose
    file NSE lacks or failed to send stays missing. Each file is fetched once for all indices,
    requests are spaced by the interval, and a rerun reads stored files from disk and asks
    again only for the days that failed."""
    far = date.max
    first = {n: min((d for d in bars if d >= (depths[n] or far)), default=far) for n in depths}

    def primary(name: str, start: date, end: date) -> Iterator[IndexPoint]:
        for day in sorted(d for d in bars if d >= first[name] and start <= d <= end):
            v = Decimal(day.toordinal())
            yield IndexPoint(day, IndexRow(name, v, v, v, v))

    now = [0.0]
    asked: list[tuple[date, float]] = []
    by_url = {history_build.INDEX_ARCHIVE_URL + index_archive_name(d): d for d in FILL_DAYS}

    def fetch(url: str) -> FetchResult:
        day = by_url[url]
        asked.append((day, now[0]))
        kind = files.get(day, usual)
        if kind in ("not_found", "error"):
            return FetchResult(kind)  # type: ignore[arg-type]
        return FetchResult("ok", _archive_csv(day, kind == "old_names"))

    def sleep(seconds: float) -> None:
        now[0] += seconds

    pacing = Pacing(interval=2.0, clock=lambda: now[0], sleep=sleep)
    failed = {d for d, kind in files.items() if kind in ("not_found", "error")}
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        _bars(store, bars)
        archive = ArchiveIndexSource(store, fetch, pacing)
        load_index_history(
            store, archive_fill_source(primary, archive), FILL_DAYS[0], FILL_DAYS[-1]
        )
        before = max(first.values())
        assert sorted(d for d, _t in asked) == sorted(d for d in bars if d < before)
        assert all(b - a >= 2.0 for (_d, a), (_e, b) in itertools.pairwise(asked))
        missing = index_coverage(store, sorted(bars), dict.fromkeys(INDEX_NAMES, FILL_DAYS[0]))
        for name in INDEX_NAMES:
            assert missing[name] == sorted(d for d in bars if d < first[name] and d in failed)
        con = store.connect()
        rows = con.execute("SELECT index_name, date, close FROM index_daily").fetchall()
        con.close()
        assert {n for n, _d, _c in rows} <= set(INDEX_NAMES)
        for name, day, close in rows:
            assert close == day.toordinal() + (Decimal(0) if day >= first[name] else Decimal("0.5"))
        asked.clear()
        again = ArchiveIndexSource(store, fetch, pacing)
        load_index_history(store, archive_fill_source(primary, again), FILL_DAYS[0], FILL_DAYS[-1])
        assert sorted(d for d, _t in asked) == sorted(d for d in bars if d < before and d in failed)


ISOLATION_DAYS = weekdays(date(2024, 6, 24), date(2024, 6, 28))


@given(
    failing=st.dictionaries(
        st.sampled_from(INDEX_NAMES), st.tuples(st.booleans(), st.integers(0, 5))
    ),
)
@settings(max_examples=30, deadline=None)
def test_index_failure_isolated(failing: dict[str, tuple[bool, int]]) -> None:
    """PR 44: an index whose source fails (an MCP error, or anything else) is recorded with a
    stable code and keeps the rows it gave before failing; every other index loads in full, the
    validation and the report still run, and the run (exit code and job) needs the operator
    exactly when an index failed."""

    def source(name: str, start: date, end: date) -> Iterator[IndexPoint]:
        mcp, after = failing.get(name, (False, len(ISOLATION_DAYS)))
        for day in weekdays(start, end)[:after]:
            v = Decimal(day.toordinal())
            yield IndexPoint(day, IndexRow(name, v, v, v, v))
        if name in failing:
            raise McpError(CODE_BAD_RESPONSE) if mcp else RuntimeError("upstream text")

    with tempfile.TemporaryDirectory() as tmp:
        args: dict[str, Any] = {
            "store": DataStore(Path(tmp)),
            "fetch": lambda _url: FetchResult("not_found"),
            "catalog": DictCatalog(auto=True),
            "start": ISOLATION_DAYS[0],
            "end": ISOLATION_DAYS[-1],
            "holidays": set(ISOLATION_DAYS),  # no gaps: only the index can fail the run
            "pacing": Pacing(interval=0),
            "index_source": source,
            "index_starts": dict.fromkeys(INDEX_NAMES, ISOLATION_DAYS[0]),
            "validation": Validation(lambda _s, _d: None),
        }
        report = run_history_build(**args)
        assert report.index is not None
        assert report.validation is not None
        assert report.index.failed == {
            n: CODE_BAD_RESPONSE if mcp else "index_source_failed"
            for n, (mcp, _after) in failing.items()
        }
        loaded = load_index_history(args["store"], lambda *_a: iter(()), *ISOLATION_DAYS[::4])
        assert loaded.failed == {}
        con = args["store"].connect()
        rows = con.execute("SELECT index_name, count(*) FROM index_daily GROUP BY 1").fetchall()
        con.close()
        want = {
            n: min(failing.get(n, (False, len(ISOLATION_DAYS)))[1], len(ISOLATION_DAYS))
            for n in INDEX_NAMES
        }
        assert dict(rows) == {n: k for n, k in want.items() if k}
        assert history_build._exit_code(report) == (1 if failing else 0)
        assert ("failed {}" in history_build._summary(report)) is not bool(failing)
        if failing:
            with pytest.raises(JobError, match="history_index_incomplete"):
                history_build_job(**args)()
        else:
            history_build_job(**args)()


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


@given(
    marks=st.dictionaries(st.sampled_from(WINDOW), st.booleans(), max_size=len(WINDOW)),
    other_market=st.booleans(),
)
@settings(max_examples=25, deadline=None)
def test_cli_passes_calendar_holidays(marks: dict[date, bool], other_market: bool) -> None:
    """PR 42: the command line hands the build the NSE calendar's non-trading dates (not the
    calendar's `holidays` method). Driving `main` over a stored calendar and a window where no
    day has a file, every NSE holiday is reported as an explained holiday and every other
    weekday as a gap, and `main` exits 1 exactly when there is a gap; another market's calendar
    rows never count. Name lookups and sockets are blocked and recorded, so a patch that stops
    applying fails the property instead of reaching NSE."""
    with tempfile.TemporaryDirectory() as tmp:
        code, report, _out, _root = _drive_main(marks, other_market, Path(tmp), "environment")
    holidays = {d for d, trading in marks.items() if not trading}
    assert set(report.days.holidays) == holidays
    assert set(report.days.gaps) == set(WINDOW) - holidays
    assert code == (1 if report.days.gaps else 0)  # a gap needs the operator; holidays do not


@given(
    marks=st.dictionaries(st.sampled_from(WINDOW), st.booleans(), max_size=len(WINDOW)),
    source=st.sampled_from(ROOT_SOURCES),
)
@settings(max_examples=25, deadline=None)
def test_cli_root_and_report(marks: dict[date, bool], source: str) -> None:
    """PR 43: the command line resolves the data root after `.env` is loaded, so a root set
    only in `.env` counts, one in the environment wins and a blank one means the default; and
    the summary it prints is also appended to `history_build/report-YYYY-MM-DD.txt` under that
    root, the only directory the run writes to."""
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        _code, _report, out, root = _drive_main(marks, False, scratch, source)
        written = sorted(p.relative_to(scratch).as_posix() for p in scratch.rglob("*.*"))
        assert {p.name for p in scratch.iterdir()} == {".env", root.name}, written
        assert (root / "build" / "state.json").is_file(), written  # the store got the root too
        saved = list((root / "history_build").glob("report-*.txt"))
        assert len(saved) == 1, written
        assert saved[0].read_text(encoding="utf-8").endswith(out)


def _drive_main(
    marks: dict[date, bool], other_market: bool, scratch: Path, source: str
) -> tuple[int, BuildReport, str, Path]:
    """`main` over a stored calendar of `marks` (and BSE rows that close every day of WINDOW
    when `other_market`), the real build with no file for any day, the data root from `source`
    in `scratch`: the exit code, the build's report, what was printed and the root it must use."""
    engine = memory_engine()
    factory = sessionmaker(bind=engine)
    with factory() as session:
        market_id = seed_market(session)
        rows = [
            HatTradingCalendar(market_id=market_id, day=d, is_trading=t) for d, t in marks.items()
        ]
        if other_market:
            bse = HatMarket(
                code="BSE",
                timezone="Asia/Kolkata",
                currency="INR",
                session_open=time(9, 15),
                session_close=time(15, 30),
            )
            session.add(bse)
            session.flush()
            rows += [HatTradingCalendar(market_id=bse.id, day=d, is_trading=False) for d in WINDOW]
        session.add_all(rows)
        session.commit()
    reports: list[BuildReport] = []
    real_build = history_build.run_history_build

    def recording_build(**kwargs: Any) -> BuildReport:  # noqa: ANN401 - main's keyword arguments
        report = real_build(**kwargs)
        reports.append(report)
        return report

    def nothing_fetcher(timeout: float = 30.0) -> Fetcher:
        return lambda _url: FetchResult("not_found")

    argv = [
        "--from",
        WINDOW[0].isoformat(),
        "--to",
        WINDOW[-1].isoformat(),
        "--interval",
        "0",
        "--skip-actions",
        "--skip-index",
        "--skip-validation",
    ]
    with (
        operator_env(scratch, source) as root,
        patch("app.database.SessionLocal", factory),
        patch.object(history_build, "run_history_build", recording_build),
        patch("app.hat.data.bhavcopy.http_fetcher", nothing_fetcher),
        patch("app.hat.data.mcp_client.client_from_env", return_value=None),
        patch("logging.basicConfig"),  # main's own logging setup must not leak into the run
        patch("sys.stdout", new_callable=io.StringIO) as out,
        # whatever is patched above, nothing may reach the network
        no_network() as reached,
    ):
        code = history_build.main(argv)
    engine.dispose()
    assert reached == []
    [report] = reports
    return code, report, out.getvalue(), root


@given(
    words=st.lists(
        st.sampled_from(["Face value split", "Bonus issue", "Consolidation of shares"]),
        max_size=3,
    ),
    split=st.booleans(),
)
def test_archive_unparsed_reported(words: list[str], split: bool) -> None:
    """HAT-DATA-055, HAT-EXEC-144: every archive row that names a split, a bonus or a
    consolidation without a usable ratio reaches the build report (`actions_unparsed`) and its
    summary; a parseable split beside them is still loaded. Harm: a consolidation in the
    archive leaves the adjusted history wrong and nobody is told."""
    rows = [f"AAA,{word},14-Jun-2018" for word in words]
    if split:
        rows.append("AAA,Face value split from Rs 10 to Rs 2,15-Jun-2018")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "actions.csv"
        path.write_text("SYMBOL,PURPOSE,EX-DATE\n" + "\n".join(rows) + "\n", encoding="utf-8")
        hooks = history_build._action_hooks(
            None,
            lambda: [(1, "AAA")],
            str(path),
            date(2018, 1, 1),
            date(2018, 12, 31),
            lambda _records: LoadReport(),
        )
        records, failed, unparsed = hooks.loader()
    assert failed == 0
    assert list(unparsed) == words
    assert len(records) == int(split)
    report = BuildReport(DownloadReport(), DayReconciliation(), PartitionReport())
    report.actions_unparsed = list(unparsed)
    assert f"archive actions not parsed {len(words)}" in history_build._summary(report)
