"""PBT history_build.* (HAT-DATA-091, 092, 093, 096; HAT-DEC-I2): the whole build runs offline on
generated bhavcopy zips in both formats across the 8 Jul 2024 switch. Completeness, no duplicate
(instrument, date), a missing day reported and left empty, and a repeated or resumed run changing
nothing."""

import hashlib
import tempfile
from collections.abc import Mapping
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from hat_bhav_support import prices, render_old, render_udiff
from hat_mcp_support import ScriptedTransport, seed_market
from hat_support import memory_engine
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import sessionmaker

from app.hat.data import history_build
from app.hat.data.bhavcopy import BhavRow, Fetcher, FetchResult, archive_url, weekdays
from app.hat.data.history_build import (
    BuildReport,
    DictCatalog,
    Pacing,
    history_build_job,
    mcp_close_source,
    mcp_index_source,
    run_history_build,
)
from app.hat.data.mcp_client import McpClient, McpError
from app.hat.data.store import DATASET_BARS, DataStore
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

    reached: list[object] = []

    def no_network(*args: object, **_kwargs: object) -> None:
        # recorded, not only raised: the real fetcher turns any exception into an "error" answer
        reached.append(args)
        raise OSError("a property reached the network")

    def nothing_fetcher(timeout: float = 30.0) -> Fetcher:
        return lambda _url: FetchResult("not_found")

    with tempfile.TemporaryDirectory() as tmp:
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
            patch("app.database.SessionLocal", factory),
            patch.object(history_build, "DataStore", return_value=DataStore(Path(tmp))),
            patch.object(history_build, "run_history_build", recording_build),
            patch("app.hat.data.bhavcopy.http_fetcher", nothing_fetcher),
            patch("app.hat.data.mcp_client.client_from_env", return_value=None),
            patch("logging.basicConfig"),  # main's own logging setup must not leak into the run
            # whatever is patched above, nothing may reach the network
            patch("socket.getaddrinfo", no_network),
            patch("socket.socket.connect", no_network),
            patch("socket.create_connection", no_network),
        ):
            code = history_build.main(argv)
    engine.dispose()
    assert reached == []
    [report] = reports
    holidays = {d for d, trading in marks.items() if not trading}
    assert set(report.days.holidays) == holidays
    assert set(report.days.gaps) == set(WINDOW) - holidays
    assert code == (1 if report.days.gaps else 0)  # a gap needs the operator; holidays do not
