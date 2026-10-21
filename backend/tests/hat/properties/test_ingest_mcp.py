"""PBT ingest.* (HAT-DATA-031, 032, 036, HAT-DEC-I1): bulk-quote batching, the concurrency
limit and the trade-date check of the MCP client."""

import math
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from hat_mcp_support import ScriptedTransport
from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.hat.data.mcp_client import McpClient, batches

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
    """HAT-DEC-I1: every batch but the last is exactly 50 symbols."""
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
