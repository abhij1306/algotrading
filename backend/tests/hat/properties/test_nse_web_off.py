"""NSE's web off (`nse_web`), the bhavcopy MCP's bars (`mcp_bars`) and the MCP transport over
replicas without affinity. Small hand-built inputs; nothing reaches a network."""

from __future__ import annotations

import json
import random
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.hat.data import mcp_bars, nse_web
from app.hat.data.fyers_master import fyers_listings
from app.hat.data.mcp_client import HttpTransport, McpTransportError

DAY = date(2026, 10, 30)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.nseindia.com/api/holiday-master?type=trading",
        "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
        "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
        "https://NSEINDIA.COM./x",
        "https://www.niftyindices.com/Backpage/getTotalReturnIndexString",
    ],
)
def test_nse_web_is_refused_unless_switched_on(url: str) -> None:
    """Harm: one request from this machine to a host NSE blocks (the owner's rule)."""
    assert nse_web.refused(url, {})
    assert nse_web.refused(url, {"HAT_NSE_WEB": "no"})
    with pytest.raises(nse_web.NseWebOffError):
        nse_web.guard(url, {})
    assert not nse_web.refused(url, {"HAT_NSE_WEB": "1"})


@pytest.mark.parametrize(
    "url",
    [
        "https://mcp.nseindia.in/bhavcopy/cm/mcp",
        "https://public.fyers.in/sym_details/NSE_CM.csv",
        "https://notnseindia.com/x",
        "https://nseindia.com.example/x",
    ],
)
def test_other_hosts_pass_the_guard(url: str) -> None:
    nse_web.guard(url, {})


FYERS = (
    "101,RELIANCE LTD,0,1,0.1,INE002A01018,s,d,,NSE:RELIANCE-EQ,10,10,1,RELIANCE,1\n"
    "102,3I INFOTECH,0,1,0.01,INE748C01038,s,d,,NSE:3IINFOLTD-BE,10,10,2,3IINFOLTD,2\n"
    "103,SOME FUT,0,1,0.1,XX,s,d,,NSE:NIFTY-INDEX,10,10,3,NIFTY,3\n"
    "104,A GSEC,0,1,0.1,IN0020230085,s,d,,NSE:GS2033-GS,10,10,4,GS2033,4\n"
)


def test_fyers_master_gives_the_isin_of_each_equity_symbol_and_series() -> None:
    listed = fyers_listings(FYERS)
    assert listed == {("RELIANCE", "EQ"): "INE002A01018", ("3IINFOLTD", "BE"): "INE748C01038"}
    isin_of = mcp_bars.isin_lookup(listed, {"OLDCO": "INE000X01011", "RELIANCE": "INE999Z01019"})
    assert isin_of("RELIANCE", "EQ") == "INE002A01018"  # the master's, over the stored one
    assert isin_of("3IINFOLTD", "EQ") == "INE748C01038"  # a series move the master lags
    assert isin_of("OLDCO", "BZ") == "INE000X01011"  # not in the master: the last session's
    assert isin_of("NEWCO", "EQ") is None
    assert mcp_bars.universe(listed, {"OLDCO": "x"}) == ["3IINFOLTD", "OLDCO", "RELIANCE"]


def _row(symbol: str, series: str = "EQ", day: str = "2026-10-30", **kw: object) -> dict:
    row = {
        "symbol": symbol,
        "series": series,
        "open": 100.0,
        "high": 105.5,
        "low": 99.05,
        "close": 104.0,
        "prevClose": 100.0,
        "volume": 1000,
        "totalTradedValue": 103456.78,
        "date": day,
    }
    return {**row, **kw}


def test_mcp_rows_are_read_as_the_bhavcopy_parser_reads_a_file() -> None:
    """The equity series only, the session's date, an ISIN, the row check, one row per ISIN by
    series rank. Harm: a bar the archive file would not give (a debt series, a wrong day, a high
    below the close) stored as raw, which is never rewritten."""
    isins = {"AAA": "INE000A01011", "BBB": "INE000B01011", "CCC": "INE000C01011"}
    rows = [
        _row("AAA"),
        _row("AAA", "BL", close=104.5, high=105.5),  # a block window: EQ ranks first
        _row("BBB", "N1"),  # a debt series
        _row("BBB", day="2026-10-29"),
        _row("CCC", high=103.0),  # high below the close
        _row("DDD"),  # no ISIN
    ]
    day = mcp_bars.parse_mcp_day(DAY, rows, lambda s, _r: isins.get(s))
    parsed = day.parsed
    assert [(r.symbol, r.series) for r in parsed.rows] == [("AAA", "EQ")]
    bar = parsed.rows[0]
    assert (bar.isin, bar.open, bar.low, bar.close) == (
        "INE000A01011",
        Decimal("100"),
        Decimal("99.05"),
        Decimal("104"),
    )
    assert bar.traded_value == Decimal("103456.78")
    assert (parsed.skipped_series, parsed.wrong_date, parsed.invalid) == (1, 1, 1)
    assert parsed.other_series == 1
    assert day.unmapped == [("DDD", "EQ")]


def test_a_stock_that_traded_the_session_before_is_missing_only_with_a_reason() -> None:
    """Checked by name, not count. Explained: the MCP's history of the symbol holds the session
    before (so today it did not trade), or the MCP does not know the symbol and Fyers no longer
    lists the ISIN (it left the market). Harm: a stock the MCP lost dropped from a session that
    is stored for good and never asked again."""
    before = date(2026, 10, 29)
    previous = {
        "INE000A01011": "AAA",  # traded today
        "INE000B01011": "BBB",  # no trade today: its history has the day before
        "INE000C01011": "CCC",  # delisted: the MCP does not know it, Fyers no longer lists it
        "INE000D01011": "DDD",  # lost: the MCP does not know it, Fyers still lists it
        "INE000E01011": "EEE",  # lost: its history lacks the day before
    }
    seen = {"AAA": {before, DAY}, "BBB": {before}, "EEE": {date(2026, 10, 28)}}
    listed = {"INE000A01011", "INE000B01011", "INE000D01011", "INE000E01011"}
    lost = mcp_bars.unexplained(previous, {"INE000A01011"}, seen, before, listed)
    assert lost == [("INE000D01011", "DDD"), ("INE000E01011", "EEE")]
    assert mcp_bars.unexplained(previous, set(previous), seen, before, listed) == []


def test_the_bhavcopy_mcp_has_a_public_default_and_can_be_turned_off() -> None:
    """Harm: an update that cannot fetch bars until the owner edits .env, or no way to stop it."""
    from app.hat.data.mcp_client import DEFAULT_ENDPOINTS, SERVER_BHAVCOPY, endpoints_from_env

    default = DEFAULT_ENDPOINTS[SERVER_BHAVCOPY]
    assert endpoints_from_env({}) == {SERVER_BHAVCOPY: default}
    assert endpoints_from_env({"HAT_MCP_BHAVCOPY_URL": " "}) == {SERVER_BHAVCOPY: default}
    assert endpoints_from_env({"HAT_MCP_BHAVCOPY_URL": "http://x.invalid"}) == {
        SERVER_BHAVCOPY: "http://x.invalid"
    }
    assert endpoints_from_env({"HAT_MCP_BHAVCOPY_URL": "off"}) == {}
    assert not nse_web.refused(default, {})


def test_a_session_is_whole_only_near_the_last_sessions_size() -> None:
    """Harm: a partition written while the MCP has loaded part of the day's bhavcopy (raw bars
    are immutable)."""
    assert mcp_bars.whole(2952, 2950)
    assert mcp_bars.whole(2700, 2950)
    assert not mcp_bars.whole(2600, 2950)
    assert not mcp_bars.whole(0, None)
    assert mcp_bars.whole(10, None)


def test_one_call_reaches_back_at_most_three_months() -> None:
    assert mcp_bars.months_for(DAY, DAY) == 1
    assert mcp_bars.months_for(date(2026, 9, 1), DAY) == 3
    with pytest.raises(ValueError, match="mcp_span_too_long"):
        mcp_bars.months_for(date(2026, 7, 1), DAY)


def test_rows_are_grouped_by_their_own_date() -> None:
    answers = {"BBB": [_row("BBB")], "AAA": [_row("AAA", day="2026-10-29"), _row("AAA")]}
    by_day = mcp_bars.rows_by_day(answers)
    assert [r["symbol"] for r in by_day[DAY]] == ["AAA", "BBB"]
    assert [r["symbol"] for r in by_day[date(2026, 10, 29)]] == ["AAA"]


class Replicas:
    """Two replicas behind one address, each request to one of them in turn: a session lives
    only on the replica that opened it, and a request for it elsewhere answers 404 (as the NSE
    servers did on 2026-11-02, about half of all requests). Each request goes to a replica at
    random (seeded)."""

    class Response:
        def __init__(self, status: int, text: str = "", headers: dict | None = None) -> None:
            self.status_code, self.text, self.headers = status, text, headers or {}

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)  # noqa: S311 - a seeded routing fixture
        self.sessions: dict[str, int] = {}
        self.requests = 0

    def post(
        self, _url: str, *, json: Mapping[str, Any], headers: Mapping[str, str], **_k: object
    ) -> Replicas.Response:
        self.requests += 1
        replica = self.rng.randrange(2)
        method, session = json.get("method"), headers.get("Mcp-Session-Id")
        if method == "initialize":
            opened = f"r{replica}-{len(self.sessions)}"
            self.sessions[opened] = replica
            body = {"jsonrpc": "2.0", "id": json["id"], "result": {"protocolVersion": "x"}}
            return self.Response(200, _text(body), {"mcp-session-id": opened})
        if self.sessions.get(session) != replica:
            return self.Response(404)
        if method == "notifications/initialized":
            return self.Response(202)
        result = {"content": [{"type": "text", "text": _text({"ok": True})}], "isError": False}
        return self.Response(200, _text({"jsonrpc": "2.0", "id": json["id"], "result": result}))


def _text(value: object) -> str:
    return json.dumps(value)


@pytest.mark.parametrize("seed", range(5))
def test_calls_succeed_across_replicas_on_pooled_sessions(seed: int) -> None:
    """Harm: half the day's calls lost to another replica's 404, the bars of a session never
    whole, or hours of retries."""
    server = Replicas(seed)
    transport = HttpTransport({"nse-bhavcopy": "http://fixture.invalid"}, post=server.post)
    answered = 0
    for _ in range(200):
        try:
            answered += transport.call("nse-bhavcopy", "get_stock_history", {}) == {"ok": True}
        except McpTransportError:
            pass  # the client's retries take the rare call every session missed
    assert answered >= 195
    assert 1 <= transport.pool_size("nse-bhavcopy") <= 4
    assert len(server.sessions) <= 40  # sessions are reused, not opened per call


def test_a_call_fails_when_no_session_is_ever_found() -> None:
    def post(_url: str, *, json: Mapping[str, Any], **_k: object) -> Replicas.Response:
        if json.get("method") == "initialize":
            body = {"jsonrpc": "2.0", "id": json["id"], "result": {}}
            return Replicas.Response(200, _text(body), {"mcp-session-id": "s"})
        return Replicas.Response(404)

    transport = HttpTransport({"nse-bhavcopy": "http://fixture.invalid"}, post=post)
    with pytest.raises(McpTransportError):
        transport.call("nse-bhavcopy", "get_stock_history", {})
