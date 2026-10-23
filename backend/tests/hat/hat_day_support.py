"""The simulated trading day of a market built from the committed
golden-replay fixture, and fakes for everything outside HAT's code that `HatRuntime` reaches
through `RuntimeParts`. Nothing here opens a connection: the feed is driven by the check, the NSE
MCP and the bhavcopy archive answer from the fixture, Telegram is `FakeTransport` and the broker
is `FakeBroker` under the Fyers adapter.

The fixture's weekday sessions are moved onto weekdays ending on the simulated days: its session
of 2025-07-07 becomes Tuesday 2026-10-20 (the first night) and 2025-07-08 becomes Wednesday
2026-10-21 (the trading day). Thursday 2026-10-22 is a holiday of the calendar.
"""

from __future__ import annotations

import csv
import json
import time as wall
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml
from hat.fakes.broker import FakeBroker
from hat_bhav_support import render_udiff
from hat_exec_support import GTT_READY, live_session
from hat_mcp_support import ScriptedTransport
from hat_telegram_support import CHAT, SECRET, FakeTransport
from sqlalchemy.orm import Session

from app.hat.clock import FixedClock
from app.hat.confighash import config_hash
from app.hat.data.bhavcopy import BhavRow, FetchResult
from app.hat.data.features import features_job
from app.hat.data.ingest import REQUIRED_INDICES
from app.hat.data.mcp_client import McpClient, McpTransportError
from app.hat.data.reference_jobs import calendar_refresh
from app.hat.data.store import BarRow, DataStore, IndexRow
from app.hat.data.subscriptions import SubscriptionRegistry
from app.hat.data.symbols import symbol_master
from app.hat.engine.portfolio.jobs import regime_job
from app.hat.engine.strategy.versions import seed_text
from app.hat.execution.fyers_adapter import FyersHatBroker
from app.hat.ids import uuid7
from app.hat.models import (
    HatBot,
    HatBrokerAccount,
    HatIndustry,
    HatInstrument,
    HatInstrumentSymbolHistory,
    HatMarket,
    HatStrategy,
    HatStrategyVersion,
    HatUniverse,
    HatUniverseMember,
    HatUniverseVersion,
)
from app.hat.risk.policy_service import seed_initial_policy
from app.hat.telegram.config import TelegramSettings
from app.hat.time import IST

__all__ = [
    "DAY_BEFORE",
    "HOLIDAY",
    "NIGHT",
    "TRADE",
    "Bar",
    "DayFeed",
    "DayMcp",
    "Market",
    "PolledTransport",
    "World",
    "bhavcopy_fetcher",
    "ist",
    "load_market",
    "seed_world",
]

FIXTURE = Path(__file__).parent / "fixtures" / "backtest_market.csv"
INDEX = "NIFTY 500"
VIX = "INDIA VIX"
SOURCE_NIGHT = date(2025, 7, 7)  # the fixture session that becomes NIGHT
SOURCE_TRADE = date(2025, 7, 8)  # ... and TRADE
DAY_BEFORE = date(2026, 10, 19)  # the last session stored before the runtime starts
NIGHT = date(2026, 10, 20)  # the first night: the MCP has the day from 18:00
TRADE = date(2026, 10, 21)  # the trading day; its night falls back to the bhavcopy at 22:00
HOLIDAY = date(2026, 10, 22)
NAMES = (
    "ALPHA",
    "BRAVO",
    "CEDAR",
    "DELTA",
    "EMBER",
    "FJORD",
    "GLADE",
    "HAVEN",
    "IVORY",
    "JASPER",
    "KESTREL",
    "LUMEN",
)
SEEDS = ("stage2-contraction-breakout", "momentum-breakout-ep")
PLACES = Decimal("0.01")


def ist(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute, second), tzinfo=IST)


@dataclass(frozen=True)
class Bar:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class Instrument:
    id: uuid.UUID
    symbol: str
    isin: str


@dataclass
class Market:
    """The fixture moved onto the simulated dates, through TRADE."""

    days: list[date]
    instruments: list[Instrument]
    bars: dict[date, dict[uuid.UUID, Bar]]
    index: dict[date, Bar]

    def by_symbol(self, symbol: str) -> Instrument:
        return next(i for i in self.instruments if i.symbol == symbol)

    def previous(self, day: date) -> date:
        return max(d for d in self.days if d < day)

    def vix(self, day: date) -> Bar:
        """A synthetic India VIX, low and easing over the sessions."""
        n = self.days.index(day)
        level = (Decimal(16) - Decimal(4) * n / len(self.days)).quantize(PLACES)
        return Bar(level, level + 1, level - 1, level, 0)

    def avg50(self, instrument_id: uuid.UUID, before: date) -> int:
        days = [d for d in self.days if d < before][-50:]
        return sum(self.bars[d][instrument_id].volume for d in days) // len(days)

    def bar_rows(self, day: date) -> list[BarRow]:
        previous = self.previous(day) if day != self.days[0] else None
        run = uuid7()
        return [
            BarRow(
                i.id,
                i.isin,
                i.symbol,
                "EQ",
                b.open,
                b.high,
                b.low,
                b.close,
                None if previous is None else self.bars[previous][i.id].close,
                b.volume,
                None,
                "bhavcopy",
                run,
            )
            for i in self.instruments
            if (b := self.bars[day].get(i.id)) is not None
        ]

    def index_rows(self, day: date) -> list[IndexRow]:
        return [
            IndexRow(name, b.open, b.high, b.low, b.close)
            for name, b in ((INDEX, self.index[day]), (VIX, self.vix(day)))
        ]


def _weekdays_ending(last: date, count: int) -> list[date]:
    days: list[date] = []
    day = last
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return days[::-1]


def load_market() -> Market:
    raw: dict[date, dict[str, Bar]] = {}
    with FIXTURE.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            day = date.fromisoformat(row["date"])
            if day > SOURCE_TRADE:
                continue
            close = row["close"]  # the index rows carry only a close
            raw.setdefault(day, {})[row["instrument_id"]] = Bar(
                *(Decimal(row[k] or close) for k in ("open", "high", "low", "close")),
                int(Decimal(row["volume"] or 0)),
            )
    source_days = sorted(raw)
    days = _weekdays_ending(TRADE, len(source_days))
    ids = sorted(k for k in raw[SOURCE_TRADE] if k != INDEX)
    instruments = [Instrument(uuid.UUID(k), NAMES[n], f"INE{n:09d}") for n, k in enumerate(ids)]
    bars = {
        mapped: {uuid.UUID(k): b for k, b in raw[source].items() if k != INDEX}
        for source, mapped in zip(source_days, days, strict=True)
    }
    index = {mapped: raw[source][INDEX] for source, mapped in zip(source_days, days, strict=True)}
    assert days[source_days.index(SOURCE_NIGHT)] == NIGHT
    return Market(days, instruments, bars, index)


# --- the store before the first night ---------------------------------------------------------


def seed_store(store: DataStore, market: Market, through: date = DAY_BEFORE) -> None:
    """The history the nightly jobs build on: bars, index levels and features through
    `through`, as the build leaves them."""
    for day in market.days:
        if day > through:
            break
        store.write_bars(day, market.bar_rows(day))
        store.upsert_index(day, market.index_rows(day))
    features_job(store, list, through=through)()


# --- the database ---------------------------------------------------------------------------


def holiday_text() -> str:
    """NSE's holiday list for 2026 with one holiday, HOLIDAY."""
    return json.dumps(
        {"CM": [{"tradingDate": HOLIDAY.strftime("%d-%b-%Y"), "description": "Simulated"}]}
    )


def band_text(market: Market) -> str:
    rows = ["Symbol,Series,Security Name,Band"]
    rows += [f"{i.symbol},EQ,{i.symbol} LTD,No Band" for i in market.instruments]
    return "\n".join(rows) + "\n"


@dataclass
class World:
    user_id: uuid.UUID
    account_id: uuid.UUID
    bots: dict[str, uuid.UUID] = field(default_factory=dict)


def seed_world(
    factory: Callable[[], Session], market: Market, user_id: uuid.UUID, store: DataStore
) -> World:
    """The market, the calendar (through the real `calendar.refresh` body), the instruments, the
    seed policy, one universe of every instrument, one active Fyers account and the two seed bots
    in shadow mode, and the regime rows of the sessions before the first night."""
    with factory() as session:
        world = _seed_rows(session, market, user_id)
    calendar_refresh(holiday_list=holiday_text, session_factory=factory)()
    with factory() as session:
        for day in [d for d in market.days if d <= DAY_BEFORE][-5:]:
            regime_job(session, store, day)
            session.commit()
    return world


def _seed_rows(session: Session, market: Market, user_id: uuid.UUID) -> World:
    nse = HatMarket(
        code="NSE",
        timezone="Asia/Kolkata",
        currency="INR",
        session_open=time(9, 15),
        session_close=time(15, 30),
    )
    session.add(nse)
    session.flush()
    first = market.days[0]
    for n, instrument in enumerate(market.instruments):
        session.add(
            HatInstrument(
                id=instrument.id,
                market_id=nse.id,
                isin=instrument.isin,
                series="EQ",
                symbol=instrument.symbol,
                name=f"{instrument.symbol} LTD",
                face_value=Decimal(1),
                listed_on=first - timedelta(days=3650),
                active=True,
            )
        )
        session.flush()
        session.add(
            HatInstrumentSymbolHistory(
                instrument_id=instrument.id, symbol=instrument.symbol, valid_from=first
            )
        )
        session.add(
            HatIndustry(
                instrument_id=instrument.id,
                sector="sector",
                industry=f"industry-{n}",
                effective_from=first,
            )
        )
    seed_initial_policy(session, user_id, datetime(2026, 1, 1, tzinfo=UTC))
    account = HatBrokerAccount(
        user_id=user_id,
        broker="fyers",
        account_ref="SIM0001",
        capabilities=GTT_READY.to_json(),
        status="active",
    )
    universe = HatUniverse(user_id=user_id, market_id=nse.id, name="sim", kind="manual")
    session.add_all([account, universe])
    session.flush()
    uversion = HatUniverseVersion(
        universe_id=universe.id, version=1, status="live", definition={}, config_hash="sim"
    )
    session.add(uversion)
    session.flush()
    for instrument in market.instruments:
        session.add(
            HatUniverseMember(
                universe_id=universe.id,
                instrument_id=instrument.id,
                effective_from=first,
                reason="constituent",
            )
        )
    world = World(user_id, account.id)
    for n, name in enumerate(SEEDS):
        config = yaml.safe_load(seed_text(name))
        strategy = HatStrategy(user_id=user_id, market_id=nse.id, name=name, persona="trader")
        session.add(strategy)
        session.flush()
        version = HatStrategyVersion(
            strategy_id=strategy.id,
            version=1,
            status="shadow",
            config=config,
            config_hash=config_hash(config),
        )
        session.add(version)
        session.flush()
        bot = HatBot(
            user_id=user_id,
            strategy_id=strategy.id,
            live_version_id=version.id,
            broker_account_id=account.id,
            execution_mode="shadow",
            status="active",
            universe_version_id=uversion.id,
            lifecycle_state="live",
            risk_cluster=f"cluster-{n}",
        )
        session.add(bot)
        session.flush()
        world.bots[name] = bot.id
    session.commit()
    return world


# --- the parts -------------------------------------------------------------------------------


class DayFeed:
    """The one live feed, faked: the check emits ticks to the listeners on the loop thread."""

    def __init__(self) -> None:
        self.registry = SubscriptionRegistry(symbol_master.to_db)
        self._listeners: list[Any] = []
        self.subscribed: set[str] = set()
        self.started = 0
        self.stopped = 0
        self._connected = False

    def add_tick_listener(self, listener: Any) -> Any:  # noqa: ANN401 - the Feed protocol
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    async def subscribe(self, symbols: list[str]) -> None:
        self.subscribed |= set(symbols)

    async def unsubscribe(self, symbols: list[str]) -> None:
        self.subscribed -= set(symbols)

    def start(self, loop: object, *, prevalidated_token: bool | None = None) -> str:
        del loop, prevalidated_token
        self.started += 1
        self._connected = True
        return "started"

    async def stop(self) -> None:
        self.stopped += 1
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def listeners(self) -> int:
        return len(self._listeners)

    def emit(
        self,
        symbol: str,
        price: Decimal,
        volume: int,
        at: datetime,
        *,
        opened: Decimal,
        prev_close: Decimal,
    ) -> None:
        """One tick to every listener: last price, cumulative volume, exchange time, and the
        day's open and previous close (the gap record reads them from the first tick)."""
        payload = {
            "symbol": symbol,
            "ltp": float(price),
            "volume": volume,
            "exch_feed_time": int(at.timestamp()),
            "open_price": float(opened),
            "prev_close_price": float(prev_close),
        }
        for listener in list(self._listeners):
            listener(dict(payload))


class PolledTransport(FakeTransport):
    """`FakeTransport` whose `getUpdates` waits a little, as long polling does, so the poller
    does not spin."""

    def call(self, method: str, payload: Mapping[str, Any], timeout: float) -> object:
        if method == "getUpdates":
            wall.sleep(0.05)
        return super().call(method, payload, timeout)


def telegram_settings() -> TelegramSettings:
    return TelegramSettings(token="sim-token", chat_id=CHAT, approval_secret=SECRET)


class DayMcp:
    """The NSE MCP servers, answering from the market and the clock. The day asked about is the
    clock's trade date; it is served from `ready_at[day]` on (never when absent), and before that
    the previous session is."""

    def __init__(self, market: Market, clock: FixedClock) -> None:
        self.market = market
        self.clock = clock
        self.ready_at: dict[date, datetime] = {NIGHT: ist(NIGHT, 18, 0)}
        self.opens: dict[str, Decimal] = {}  # the gap scan's open prices of TRADE
        self.calls: list[str] = []

    def client(self) -> McpClient:
        return McpClient(ScriptedTransport(self.respond), sleep=lambda _s: None)

    def _served(self) -> date:
        now = self.clock.now()
        day = now.astimezone(IST).date()
        ready = self.ready_at.get(day)
        return day if ready is not None and now >= ready else self.market.previous(day)

    def respond(self, tool: str, arguments: Mapping[str, Any]) -> object:
        self.calls.append(tool)
        if tool == "get_bulk_quote":
            day = self._served()
            return {"quotes": [self._quote(s, day) for s in arguments["symbols"]]}
        if tool == "get_index_snapshot":
            return self._indices(date.fromisoformat(arguments["date"]))
        if tool == "get_market_breadth":
            return self._breadth(date.fromisoformat(arguments["date"]))
        if tool == "get_corporate_actions":
            return {"symbol": arguments["symbol"], "actions": []}
        if tool == "cm_get_equity_stocks":
            return {"stocks": self._listing(str(arguments["symbolFilter"]))}
        raise McpTransportError()

    def _quote(self, symbol: str, day: date) -> dict[str, Any]:
        found = [i for i in self.market.instruments if i.symbol == symbol]
        if not found:  # the probe's RELIANCE: only its date is read
            return {"symbol": symbol, "date": day.isoformat()}
        bar = self.market.bars[day][found[0].id]
        prev = self.market.bars[self.market.previous(day)][found[0].id].close
        return {
            "symbol": symbol,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "prev_close": float(prev),
            "volume": bar.volume,
            "date": day.isoformat(),
        }

    def _indices(self, day: date) -> dict[str, Any]:
        if day not in self.market.index:
            raise McpTransportError()
        rows = self.market.index_rows(day)
        assert {r.index_name for r in rows} == set(REQUIRED_INDICES)
        return {
            "date": day.isoformat(),
            "indices": [
                {
                    "indexName": r.index_name,
                    "open": float(r.open),
                    "high": float(r.high),
                    "low": float(r.low),
                    "close": float(r.close),
                }
                for r in rows
            ],
        }

    def _breadth(self, day: date) -> dict[str, Any]:
        previous = self.market.previous(day)
        moves = [
            b.close - self.market.bars[previous][iid].close
            for iid, b in self.market.bars[day].items()
        ]
        return {
            "date": day.isoformat(),
            "total_stocks": len(moves),
            "advances": sum(1 for m in moves if m > 0),
            "declines": sum(1 for m in moves if m < 0),
            "unchanged": sum(1 for m in moves if m == 0),
        }

    def _listing(self, prefix: str) -> list[dict[str, Any]]:
        previous = self.market.previous(TRADE)
        return [
            {
                "symbol": i.symbol,
                "series": "EQ",
                "openPrice": float(self.opens.get(i.symbol, Decimal(0))),
                "preClosePrice": float(self.market.bars[previous][i.id].close),
            }
            for i in self.market.instruments
            if i.symbol.startswith(prefix) and i.symbol in self.opens
        ]


def bhavcopy_fetcher(market: Market, clock: FixedClock, urls: list[str]) -> Any:  # noqa: ANN401
    """The NSE archive: the clock's trade date as a UDiFF bhavcopy, every URL recorded."""

    def fetch(url: str) -> FetchResult:
        urls.append(url)
        day = clock.now().astimezone(IST).date()
        if day not in market.bars:
            return FetchResult("not_found")
        previous = market.previous(day)
        rows = [
            BhavRow(
                day,
                i.isin,
                "EQ",
                i.symbol,
                b.open,
                b.high,
                b.low,
                b.close,
                market.bars[previous][i.id].close,
                b.volume,
                (b.close * b.volume).quantize(PLACES),
            )
            for i in market.instruments
            if (b := market.bars[day].get(i.id)) is not None
        ]
        return FetchResult("ok", render_udiff(day, rows))

    return fetch


def broker_for(market: Market, fake: FakeBroker) -> Any:  # noqa: ANN401
    """Every account gets the Fyers adapter over `fake`, with a live session."""
    pairs = [(i.symbol, f"NSE:{i.symbol}-EQ") for i in market.instruments]

    def build(_account: HatBrokerAccount) -> FyersHatBroker:
        broker = FyersHatBroker(fake, instruments=lambda: list(pairs), capabilities=GTT_READY)  # type: ignore[arg-type]
        live_session(SimpleNamespace(broker=broker))  # type: ignore[arg-type]
        return broker

    return build
