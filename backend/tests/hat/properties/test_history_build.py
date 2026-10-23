"""history_build's pure seams: the MCP index and close wrappers, the index archive fill and the
corporate-action archive parse. The build itself is verified on real data, never here."""

import itertools
import tempfile
from collections.abc import Iterator, Mapping
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hat_mcp_support import ScriptedTransport
from hypothesis import given, settings
from hypothesis import strategies as st

from app.hat.data import history_build
from app.hat.data.bhavcopy import (
    DayReconciliation,
    DownloadReport,
    FetchResult,
    weekdays,
)
from app.hat.data.corporate_actions import LoadReport
from app.hat.data.history_build import (
    CODE_INDEX_ARCHIVE,
    INDEX_NAMES,
    ArchiveIndexSource,
    BuildReport,
    IndexPoint,
    Pacing,
    archive_fill_source,
    index_archive_name,
    load_index_history,
    mcp_close_source,
    mcp_index_source,
)
from app.hat.data.mcp_client import CODE_BAD_RESPONSE, CODE_TIMEOUT, McpClient, McpError
from app.hat.data.store import (
    BarRow,
    DataStore,
    IndexRow,
    PartitionReport,
    index_coverage,
)

# --- the MCP history wrappers ---------------------------------------

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
    """A server that keeps history only from `depth` answers an older window with its
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
        st.sampled_from(FILL_DAYS),
        st.sampled_from(["ok", "old_names", "not_found", "error", "garbage"]),
    ),
    usual=st.sampled_from(["ok", "old_names"]),
    broken=st.sets(st.sampled_from(INDEX_NAMES)),
)
@settings(max_examples=40, deadline=None)
def test_archive_fills_before_mcp_depth(
    bars: set[date],
    depths: dict[str, date | None],
    files: dict[date, str],
    usual: str,
    broken: set[str],
) -> None:
    """With the MCP serving each index only from its depth (or not at all), the daily
    `ind_close_all` archive fills every earlier day that has bars, under today's index names
    whatever the file calls them, and the MCP's rows win from its depth on. Only a day whose
    file NSE lacks or failed to send stays missing. Each file is fetched once for all indices,
    requests are spaced by the interval, and a rerun reads stored files from disk and asks
    again only for the days that failed. A failed fetch, or an answer that is no index file
    (never kept), fails the indices that needed that day; an MCP failing after its first rows
    still gets the archive fill and is reported with its own code."""
    far = date.max
    first = {n: min((d for d in bars if d >= (depths[n] or far)), default=far) for n in depths}

    def primary(name: str, start: date, end: date) -> Iterator[IndexPoint]:
        for day in sorted(d for d in bars if d >= first[name] and start <= d <= end):
            v = Decimal(day.toordinal())
            yield IndexPoint(day, IndexRow(name, v, v, v, v))
        if name in broken:
            raise McpError(CODE_TIMEOUT)

    now = [0.0]
    asked: list[tuple[date, float]] = []
    by_url = {history_build.INDEX_ARCHIVE_URL + index_archive_name(d): d for d in FILL_DAYS}

    def fetch(url: str) -> FetchResult:
        day = by_url[url]
        asked.append((day, now[0]))
        kind = files.get(day, usual)
        if kind in ("not_found", "error"):
            return FetchResult(kind)  # type: ignore[arg-type]
        if kind == "garbage":
            return FetchResult("ok", b"<html>Access Denied</html>")
        return FetchResult("ok", _archive_csv(day, kind == "old_names"))

    def sleep(seconds: float) -> None:
        now[0] += seconds

    pacing = Pacing(interval=2.0, clock=lambda: now[0], sleep=sleep)
    errors = {d for d, kind in files.items() if kind in ("error", "garbage")}
    failed = errors | {d for d, kind in files.items() if kind == "not_found"}
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        _bars(store, bars)
        archive = ArchiveIndexSource(store, fetch, pacing)
        report = load_index_history(
            store, archive_fill_source(primary, archive), FILL_DAYS[0], FILL_DAYS[-1]
        )
        assert report.failed == {
            n: CODE_TIMEOUT if n in broken else CODE_INDEX_ARCHIVE
            for n in INDEX_NAMES
            if n in broken or any(d < first[n] for d in bars & errors)
        }
        before = max(first.values())
        assert sorted(d for d, _t in asked) == sorted(d for d in bars if d < before)
        assert all(t >= 2.0 for _d, t in asked[:1])  # the first request waits its turn too
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
    words=st.lists(
        st.sampled_from(["Face value split", "Bonus issue", "Consolidation of shares"]),
        max_size=3,
    ),
    split=st.booleans(),
)
def test_archive_unparsed_reported(words: list[str], split: bool) -> None:
    """Every archive row that names a split, a bonus or a
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
