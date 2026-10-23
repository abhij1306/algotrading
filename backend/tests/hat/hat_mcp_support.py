"""Fakes for the verification suites: a scripted MCP transport, a fake cm-market listing
server and helpers to build a database holding the reference tables. No network anywhere."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from datetime import time as clock_time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from hat_support import memory_engine
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.hat.data.mcp_client import McpTransportError
from app.hat.data.reference import Security
from app.hat.models import (
    HatBrokerSymbol,
    HatIndustry,
    HatInstrument,
    HatInstrumentSymbolHistory,
    HatJobRun,
    HatMarket,
    HatPriceBand,
    HatTradingCalendar,
)

FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE_TABLES = (
    HatMarket,
    HatInstrument,
    HatInstrumentSymbolHistory,
    HatBrokerSymbol,
    HatPriceBand,
    HatIndustry,
    HatTradingCalendar,
    HatJobRun,
)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> dict[str, Any]:
    return json.loads(fixture_text(name))


class ScriptedTransport:
    """Plays one outcome per call: a value is returned, an exception is raised. Counts the
    calls in flight and the most there ever were."""

    def __init__(
        self,
        outcomes: Sequence[Any] | Callable[[str, Mapping[str, Any]], Any],
        *,
        hold: float = 0.0,
    ) -> None:
        self._outcomes = outcomes
        self._hold = hold
        self._lock = threading.Lock()
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    def call(self, server: str, tool: str, arguments: Mapping[str, Any]) -> object:
        with self._lock:
            index = len(self.calls)
            self.calls.append((server, tool, dict(arguments)))
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self._hold:
                time.sleep(self._hold)
            outcome = (
                self._outcomes(tool, arguments)
                if callable(self._outcomes)
                else self._outcomes[min(index, len(self._outcomes) - 1)]
            )
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        finally:
            with self._lock:
                self.in_flight -= 1


class ListingServer:
    """A fake cm-market `cm_get_equity_stocks`: rows sorted by symbol, filtered by prefix and
    cut at `limit` (the real server's cap is 500)."""

    def __init__(self, listed: Sequence[tuple[str, str]], *, fail: bool = False) -> None:
        self.listed = sorted(set(listed))
        self.fail = fail
        self.requests: list[tuple[str, int]] = []
        self._lock = threading.Lock()

    def call(self, server: str, tool: str, arguments: Mapping[str, Any]) -> object:
        if self.fail:
            raise McpTransportError()
        prefix, limit = arguments["symbolFilter"], min(int(arguments["limit"]), 500)
        with self._lock:
            self.requests.append((prefix, limit))
        rows = [
            {"symbol": s, "series": series} for s, series in self.listed if s.startswith(prefix)
        ]
        return {"segment": "EQUITY", "returned": len(rows[:limit]), "stocks": rows[:limit]}


def reference_engine() -> Engine:
    return memory_engine(*REFERENCE_TABLES)


def seed_market(session: Session) -> UUID:
    market = HatMarket(
        code="NSE",
        timezone="Asia/Kolkata",
        currency="INR",
        session_open=clock_time(9, 15),
        session_close=clock_time(15, 30),
    )
    session.add(market)
    session.flush()
    return market.id


def security(
    isin: str,
    series: str = "EQ",
    symbol: str = "SYM",
    *,
    name: str | None = None,
    face_value: int = 10,
    listed_on: date | None = None,
) -> Security:
    return Security(
        isin=isin,
        series=series,
        symbol=symbol,
        name=name or symbol + " Ltd",
        face_value=Decimal(face_value),
        listed_on=listed_on,
    )


def equity_list_csv(securities: Sequence[Security]) -> str:
    header = (
        "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT,"
        " ISIN NUMBER, FACE VALUE"
    )
    months = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
    lines = [header]
    for s in securities:
        listed = s.listed_on or date(2000, 1, 1)
        stamp = f"{listed.day:02d}-{months[listed.month - 1]}-{listed.year}"
        name = s.name.replace(",", " ")
        lines.append(f"{s.symbol},{name},{s.series},{stamp},10,1,{s.isin},{s.face_value}")
    return "\n".join(lines) + "\n"


class StreamableServer:
    """A fake NSE MCP endpoint behaving as the live ones did on 2026-10-22: a request without a
    session it opened and initialized answers 400 (the bare `tools/call` the old client sent), a
    session is opened by `initialize` (id in the `mcp-session-id` reply header) and readied by
    `notifications/initialized` (one step of a handshake answers 404 when `lose_handshake` says
    another replica took it), and a `tools/call` whose session `lose` says another replica holds
    answers 404 and forgets it. Tool results are the recorded replies, as text content."""

    def __init__(
        self,
        replies: Mapping[str, object],
        lose: Callable[[], bool],
        lose_handshake: Callable[[], bool] = lambda: False,
    ) -> None:
        self.replies = dict(replies)
        self.lose = lose
        self.lose_handshake = lose_handshake  # a handshake step answered by another replica
        self.handshake_404s = 0
        self._handshake_lost = False  # a replica took one step of the handshake under way
        self.opened: list[str] = []
        self.handshakes = 0
        self.lost: set[str] = set()
        self.ready: set[str] = set()
        self.calls: list[tuple[str, int]] = []  # (session id, status) of every tools/call
        self.bad_requests = 0
        self._lock = threading.Lock()

    class Response:
        def __init__(
            self, status: int, text: str = "", headers: dict[str, str] | None = None
        ) -> None:
            self.status_code, self.text, self.headers = status, text, headers or {}

    def post(
        self, _url: str, *, json: Mapping[str, Any], headers: Mapping[str, str], **_k: object
    ) -> StreamableServer.Response:
        with self._lock:
            return self._answer(json, headers.get("Mcp-Session-Id"))

    def _lose_step(self) -> bool:
        """At most one step of one handshake goes to another replica."""
        if self._handshake_lost or not self.lose_handshake():
            return False
        self._handshake_lost = True
        self.handshake_404s += 1
        return True

    def _answer(self, body: Mapping[str, Any], session: str | None) -> StreamableServer.Response:
        method = body.get("method")
        if method == "initialize":
            self.handshakes += 1
            if self._lose_step():
                return self.Response(404)
            opened = f"s{self.handshakes}"
            self.opened.append(opened)
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}
            envelope = {"jsonrpc": "2.0", "id": body["id"], "result": result}
            return self.Response(200, json_text(envelope), {"mcp-session-id": opened})
        if session in self.lost:  # the live servers answer a forgotten session with 404
            self.calls.append((session, 404))
            return self.Response(404)
        if session not in self.opened:
            self.bad_requests += 1
            return self.Response(400, '{"cause":null,"stackTrace":[]}')
        if method == "notifications/initialized":
            if self._lose_step():
                self.opened.remove(session)
                return self.Response(404)
            self._handshake_lost = False
            self.ready.add(session)
            return self.Response(202)
        if method != "tools/call" or session not in self.ready:
            self.bad_requests += 1
            return self.Response(400, '{"cause":null,"stackTrace":[]}')
        if self.lose():
            self.opened.remove(session)
            self.lost.add(session)
            self.calls.append((session, 404))
            return self.Response(404)
        self.calls.append((session, 200))
        text = json_text(self.replies[body["params"]["name"]])
        result = {"content": [{"type": "text", "text": text}], "isError": False}
        return self.Response(200, json_text({"jsonrpc": "2.0", "id": body["id"], "result": result}))


def json_text(value: object) -> str:
    return json.dumps(value)
