"""Fakes for the nightly ingest and quality gate suites: a universe in an in-memory database, a
scripted NSE MCP server that answers from a clock, a store in a temporary directory. No network."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from hat_mcp_support import ScriptedTransport, fixture_json
from sqlalchemy.orm import Session, sessionmaker

from app.hat.clock import FixedClock
from app.hat.data.bhavcopy import ParsedDay
from app.hat.data.ingest import IngestDeps
from app.hat.data.mcp_client import McpClient, McpTransportError
from app.hat.data.store import DataStore
from app.hat.notify import RecordingNotifier
from app.hat.time import IST

TRADE_DAY = date(2026, 10, 20)
INDEX_ROWS = [
    {"indexName": "NIFTY 50", "open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0},
    {"indexName": "Nifty 500", "open": 200.0, "high": 210.0, "low": 190.0, "close": 205.0},
    {"indexName": "India VIX", "open": 13.0, "high": 15.0, "low": 12.0, "close": 14.0},
]


def day_at(hour: int, minute: int = 0, day: date = TRADE_DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def sleeper(clock: FixedClock) -> Callable[[float], None]:
    """A sleep that moves the fixed clock instead of waiting."""
    return lambda seconds: clock.advance(timedelta(seconds=seconds))


class Crash(BaseException):  # noqa: N818 - stands for a process that is killed mid-run
    """Not an `Exception`: nothing in the job path may catch it."""


@dataclass
class Quote:
    close: Decimal = Decimal("100")
    prev_close: Decimal | None = Decimal("100")
    volume: int = 1000

    def row(self, symbol: str, day: date) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "open": float(self.close),
            "high": float(self.close),
            "low": float(self.close),
            "close": float(self.close),
            "prev_close": None if self.prev_close is None else float(self.prev_close),
            "volume": self.volume,
            "date": day.isoformat(),
        }


class Nse:
    """A scripted `nse-bhavcopy` server. It answers with the trade date once the clock has
    reached `ready_at` (never when None), and with the day before until then."""

    def __init__(self, clock: FixedClock, day: date = TRADE_DAY) -> None:
        self.clock = clock
        self.day = day
        self.ready_at: datetime | None = day_at(18, 0, day)
        self.quotes: dict[str, Quote] = {}
        self.default = Quote()
        self.index_rows: list[dict[str, Any]] | None = list(INDEX_ROWS)
        self.index_date: date | None = day
        # the reply recorded from the live server (snake_case), moved to `day`
        self.breadth: dict[str, Any] | None = {
            **fixture_json("mcp_market_breadth.json"),
            "date": day.isoformat(),
        }
        self.actions: dict[str, list[dict[str, Any]]] = {}
        self.fail_bulk = False
        self.drop: set[str] = set()  # symbols the server returns nothing for
        self.crash = False  # raise a BaseException on a bulk quote, as a killed process would
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def ready(self) -> bool:
        return self.ready_at is not None and self.clock.now() >= self.ready_at

    def respond(self, tool: str, arguments: Mapping[str, Any]) -> object:
        self.calls.append((tool, dict(arguments)))
        if tool == "get_bulk_quote":
            if self.crash and len(arguments["symbols"]) > 1:
                raise Crash
            if self.fail_bulk and len(arguments["symbols"]) > 1:
                raise McpTransportError()
            data_day = self.day if self.ready() else self.day - timedelta(days=1)
            rows = [
                self.quotes.get(s, self.default).row(s, data_day)
                for s in arguments["symbols"]
                if s not in self.drop
            ]
            return {"count": len(rows), "quotes": rows}
        if tool == "get_index_snapshot":
            if self.index_rows is None:
                raise McpTransportError()
            return {
                "date": (self.index_date or self.day).isoformat(),
                "indices": self.index_rows,
            }
        if tool == "get_market_breadth":
            if self.breadth is None:
                raise McpTransportError()
            return self.breadth
        if tool == "get_corporate_actions":
            return {
                "symbol": arguments["symbol"],
                "actions": self.actions.get(arguments["symbol"], []),
            }
        raise McpTransportError()

    def tools(self, name: str) -> list[dict[str, Any]]:
        return [a for t, a in self.calls if t == name]


@dataclass
class World:
    """A universe, a store and the injected parts of `IngestDeps`, in a temporary directory."""

    engine: Any
    factory: sessionmaker[Session]
    store: DataStore
    clock: FixedClock
    nse: Nse
    notifier: RecordingNotifier
    ids: dict[str, uuid.UUID] = field(default_factory=dict)
    fallback_calls: list[date] = field(default_factory=list)
    fallback_day: ParsedDay | None = None
    index_csv_text: str | None = None
    rss: set[str] = field(default_factory=set)
    transport: ScriptedTransport | None = None

    def deps(self) -> IngestDeps:
        self.transport = ScriptedTransport(self.nse.respond)
        client = McpClient(self.transport, sleep=lambda _s: None)

        def fallback(day: date) -> ParsedDay | None:
            self.fallback_calls.append(day)
            return self.fallback_day

        return IngestDeps(
            client=client,
            store=self.store,
            session_factory=self.factory,
            clock=self.clock,
            sleep=sleeper(self.clock),
            fallback=fallback,
            index_csv=lambda _d: self.index_csv_text,
            rss_symbols=lambda _d: self.rss,
            notifier=self.notifier,
        )


def symbols_of(count: int) -> list[tuple[str, str, bool]]:
    return [(f"S{i:04d}", "EQ", True) for i in range(count)]
