"""history_build's pure seams: the MCP index and close wrappers and the corporate-action
archive parse. The build itself is verified on real data, never here."""

import tempfile
from collections.abc import Mapping
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
    weekdays,
)
from app.hat.data.corporate_actions import LoadReport
from app.hat.data.history_build import (
    BuildReport,
    mcp_close_source,
    mcp_index_source,
)
from app.hat.data.mcp_client import CODE_BAD_RESPONSE, McpClient, McpError
from app.hat.data.store import (
    PartitionReport,
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
