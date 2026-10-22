"""DB `ops.simulated_day`: the PR 17 acceptance check (10-ops §1-§3).

`HatRuntime` starts with HAT enabled on PostgreSQL, over the fixture market (`hat_day_support`),
`FakeBroker` under the Fyers adapter, a recording Telegram transport and a faked feed, at 17:55
IST on Tuesday 2026-10-20, and is driven on a fixed clock through:

- the first night: the MCP has the day at 18:00 and the pipeline runs in its `after` order to
  Wednesday's watchlist (one name per seed bot);
- Wednesday's session: the housekeeping, the login prompt, `live.prepare`, `modes.apply`, the gap
  scan, and ticks every 15 seconds. The momentum bot's watchlisted name breaks out on volume
  (a shadow entry) and later trades through its low-of-day stop (a live hard-stop exit); the
  stage-2 bot's name crosses its pivot on thin volume (no entry, so the row expires at 15:30);
  a 12% gapper with a neglected history becomes an episodic-pivot entry that stays open through
  the 15:20-15:25 close exits. Then the session-end jobs and the after-close reports;
- Wednesday's night: the MCP never has the day, so the ingest waits to 22:00, falls back to the
  bhavcopy and the pipeline scans for Friday;
- Thursday, a holiday: only the every-day housekeeping runs;

and stops, leaving no task behind. Shadow mode sends nothing to the broker but reads.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hat.fakes.broker import FakeBroker
from hat_day_support import (
    HOLIDAY,
    NIGHT,
    TRADE,
    Bar,
    DayFeed,
    DayMcp,
    Market,
    PolledTransport,
    World,
    band_text,
    bhavcopy_fetcher,
    broker_for,
    holiday_text,
    ist,
    load_market,
    seed_store,
    seed_world,
    telegram_settings,
)
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.hat.clock import FixedClock
from app.hat.daily.nightly import NIGHTLY_CHAIN, TextSources
from app.hat.daily.runtime import HatRuntime, RuntimeParts
from app.hat.data.store import DataStore
from app.hat.engine import lifecycle
from app.hat.models import (
    HatEvent,
    HatJobRun,
    HatPosition,
    HatSignal,
    HatTelegramMessage,
    HatWatchlistEntry,
)
from app.hat.ops.scheduler import Days, runs_on
from app.hat.settings import HatSettings
from app.hat.time import IST

pytestmark = pytest.mark.postgres

USER = uuid.UUID("00000000-0000-7000-8000-0000000000d1")
START = ist(NIGHT, 17, 55)
END = ist(HOLIDAY, 23, 55)
STEP = timedelta(seconds=15)  # in the busy hours: ticks, a scheduler tick, the periodic pass
QUIET = timedelta(minutes=5)  # outside them
BUSY_FROM = time(8, 25)
BUSY_UNTIL = time(16, 0)
OPEN = time(9, 15)
CLOSE = time(15, 30)
CLOSE_FROM = time(15, 20)
CLOSE_UNTIL = time(15, 25)
STOPPED_BY = time(11, 0)  # S2 trades through its stop at 10:58:35
SESSION_SECONDS = 375 * 60
SETTLE_SECONDS = 2.0  # what a task started before `stop` may take to end after it
TICK = Decimal("0.05")
SUCCEEDED = "succeeded"
SHADOW = "shadow"
WATCHLIST = "watchlist"
EXPECTED_PROBES = 24  # 18:00, 18:10, ... 21:50: the night the MCP never has the day

S1_NAME = "BRAVO"  # stage2-contraction-breakout watchlists it (pivot 2393.98)
S2_NAME = "JASPER"  # momentum-breakout-ep watchlists it (consolidation high 639.09)
EP_NAME = "DELTA"  # 63-day return 12%: neglected, so a 12% gap makes it an episodic pivot
P = Decimal
# The trading day's prices: (minutes after 09:15, price), linear in between; every other name
# trades flat at its previous close. Each of these names' TRADE bar is made from its path.
PATHS: dict[str, list[tuple[int, Decimal]]] = {
    S1_NAME: [(0, P(2380)), (5, P(2390)), (10, P(2402)), (375, P(2420))],
    S2_NAME: [
        (0, P(630)),
        (4, P(636)),  # the opening range high: below the consolidation high
        (5, P(635)),
        (10, P(642)),  # breaks 639.09 within the 2% chase
        (100, P(645)),
        (105, P(624)),  # through the low-of-day stop 629.95
        (375, P(626)),
    ],
    EP_NAME: [(0, P("748.90")), (4, P(752)), (5, P(750)), (10, P(756)), (375, P(760))],
}
# Day volume as a multiple of the 50-day average pace: S1's breakout has no volume behind it.
PACE = {S1_NAME: 1, S2_NAME: 3, EP_NAME: 25}
BROKER_READS = frozenset(
    {
        "fetch_orderbook",
        "fetch_tradebook",
        "fetch_holdings",
        "fetch_funds",
        "fetch_gtt_orders",
        "get_positions",
    }
)


@dataclass
class Rig:
    clock: FixedClock
    factory: sessionmaker[Session]
    market: Market
    world: World
    runtime: HatRuntime
    feed: DayFeed
    mcp: DayMcp
    fake: FakeBroker
    transport: PolledTransport
    bhav_urls: list[str] = field(default_factory=list)
    leftover: set[asyncio.Task[Any]] = field(default_factory=set)


def _truncate(engine: Engine) -> None:
    with engine.begin() as connection:
        names = connection.execute(
            text("SELECT tablename FROM pg_tables WHERE tablename LIKE 'hat\\_%' ESCAPE '\\'")
        ).scalars()
        listed = ", ".join(f'"{name}"' for name in names)
        if listed:
            connection.execute(text(f"TRUNCATE {listed} CASCADE"))


@pytest.fixture
def clean_db(pg_engine: Engine) -> Generator[Engine, None, None]:
    """The day commits from worker threads, so it cannot run inside one rolled-back
    transaction: the HAT tables are emptied before and after."""
    _truncate(pg_engine)
    yield pg_engine
    _truncate(pg_engine)


# --- the trading day's prices ---------------------------------------------------------------


def _minutes(at: datetime) -> Decimal:
    return Decimal((at - ist(TRADE, 9, 15)).total_seconds()) / 60


def _on_path(path: list[tuple[int, Decimal]], minutes: Decimal) -> Decimal:
    for (m0, p0), (m1, p1) in zip(path, path[1:], strict=False):
        if minutes <= m1:
            share = max((minutes - m0) / (m1 - m0), Decimal(0))
            return (p0 + (p1 - p0) * share).quantize(TICK)
    return path[-1][1]


def _price(market: Market, symbol: str, at: datetime) -> Decimal:
    path = PATHS.get(symbol)
    if path is None:
        return market.bars[market.previous(TRADE)][market.by_symbol(symbol).id].close
    return _on_path(path, _minutes(at))


def _volume(market: Market, symbol: str, at: datetime) -> int:
    elapsed = Decimal((at - ist(TRADE, 9, 15)).total_seconds()) / SESSION_SECONDS
    average = market.avg50(market.by_symbol(symbol).id, TRADE)
    return int(average * PACE.get(symbol, 1) * min(max(elapsed, Decimal(0)), Decimal(1)))


def _script_trade_day(market: Market, mcp: DayMcp) -> None:
    """The scripted names' TRADE bars (what the MCP and the bhavcopy report that night) follow
    their paths; the gap scan's listing opens every name at its first price."""
    closes = ist(TRADE, 15, 30)
    for symbol, path in PATHS.items():
        prices = [p for _, p in path]
        market.bars[TRADE][market.by_symbol(symbol).id] = Bar(
            prices[0], max(prices), min(prices), prices[-1], _volume(market, symbol, closes)
        )
    for instrument in market.instruments:
        mcp.opens[instrument.symbol] = _price(market, instrument.symbol, ist(TRADE, 9, 15))


def _emit_ticks(rig: Rig, at: datetime) -> None:
    market = rig.market
    previous = market.previous(TRADE)
    for instrument in market.instruments:
        symbol = instrument.symbol
        rig.feed.emit(
            symbol,
            _price(market, symbol, at),
            _volume(market, symbol, at),
            at,
            opened=rig.mcp.opens[symbol],
            prev_close=market.bars[previous][instrument.id].close,
        )


# --- driving the clock ----------------------------------------------------------------------


def _local(at: datetime) -> tuple[date, time]:
    local = at.astimezone(IST)
    return local.date(), local.time()


def _busy(at: datetime) -> bool:
    day, now = _local(at)
    return day == TRADE and BUSY_FROM <= now < BUSY_UNTIL


def _market_open(at: datetime) -> bool:
    day, now = _local(at)
    return day == TRADE and OPEN <= now < CLOSE


async def _drive(rig: Rig, until: datetime) -> None:
    """Step the fixed clock to `until`: ticks while the market is open, the live data step, a
    scheduler tick (every job it starts run to its end; the ingest's wait moves the clock) and
    the periodic pass."""
    runtime = rig.runtime
    scheduler = runtime.scheduler
    assert scheduler is not None
    while rig.clock.now() < until:
        now = rig.clock.now()
        if _market_open(now):
            _emit_ticks(rig, now)
        if runtime.live.step():
            await asyncio.sleep(0.05)  # the trigger writes run on the default executor
        await scheduler.tick()
        await scheduler.drain()
        await asyncio.to_thread(runtime.periodic, rig.clock.now())
        rig.clock.set(max(rig.clock.now(), now + (STEP if _busy(now) else QUIET)))


async def _simulate(rig: Rig) -> None:
    runtime = rig.runtime
    before = asyncio.all_tasks()
    assert await runtime.start()
    scheduler = runtime.scheduler
    assert scheduler is not None
    # The check drives the scheduler, the live data and the periodic pass on the fixed clock;
    # the Telegram poller and the relay keep running on their own.
    for task in (scheduler._task, runtime._tasks[0], runtime._tasks[1]):
        assert task is not None
        task.cancel()
    await _drive(rig, END)
    await runtime.stop()
    await asyncio.sleep(0)  # the unsubscribe calls `stop` handed to the loop start here
    started = asyncio.all_tasks() - before
    if started:
        await asyncio.wait(started, timeout=SETTLE_SECONDS)
    rig.leftover = {t for t in started if not t.done()}


# --- reading the outcome --------------------------------------------------------------------


def _runs(rig: Rig, day: date) -> list[HatJobRun]:
    with rig.factory() as session:
        return list(
            session.scalars(
                select(HatJobRun)
                .where(HatJobRun.trade_date == day)
                .order_by(HatJobRun.started_at, HatJobRun.id)
            )
        )


def _events(rig: Rig, event_type: str) -> list[HatEvent]:
    with rig.factory() as session:
        return list(
            session.scalars(
                select(HatEvent).where(HatEvent.event_type == event_type).order_by(HatEvent.id)
            )
        )


def _ingest_report(rig: Rig, day: date) -> dict[str, Any]:
    found = [e.payload for e in _events(rig, "ingest.completed")]
    return next(p for p in found if p["trade_date"] == day.isoformat())


def _trading_jobs(rig: Rig, day: date) -> set[str]:
    return {j.name for j in rig.runtime.jobs if runs_on(j, day, rig.runtime.trading)}


def _check_night(rig: Rig, day: date, source: str) -> None:
    """The pipeline of `day` ran in its `after` order and every step succeeded (HAT-OPS-021);
    the ingest took the day from `source` (HAT-OPS-022)."""
    runs = {r.job: r for r in _runs(rig, day)}
    chain = [runs[name] for name in NIGHTLY_CHAIN]
    for run in chain:
        assert run.status == SUCCEEDED, (run.job, run.error)
    starts = [r.started_at for r in chain]
    assert starts == sorted(starts)
    assert _ingest_report(rig, day)["source"] == source


def _check_trading_day_jobs(rig: Rig) -> None:
    """Every job of a trading day ran once for TRADE and succeeded (the 10-ops §3 table)."""
    runs = _runs(rig, TRADE)
    failed = {(r.job, r.error) for r in runs if r.status != SUCCEEDED}
    assert failed == set()
    assert len({r.job for r in runs}) == len(runs)
    assert {r.job for r in runs} == _trading_jobs(rig, TRADE)


def _check_holiday(rig: Rig) -> None:
    """HAT-OPS-012: on the holiday only the every-day housekeeping runs."""
    every = {j.name for j in rig.runtime.jobs if j.days is Days.EVERY and j.weekday is None}
    assert {r.job for r in _runs(rig, HOLIDAY)} == every
    assert _trading_jobs(rig, HOLIDAY) == every


def _signal_of(session: Session, symbol: str, market: Market) -> HatSignal | None:
    return session.scalar(
        select(HatSignal).where(
            HatSignal.trade_date == TRADE,
            HatSignal.instrument_id == market.by_symbol(symbol).id,
        )
    )


def _check_watchlist(rig: Rig) -> None:
    """The first night's watchlist ends the session as Triggered (S2) and Expired (S1, which
    never triggered: HAT-EXEC-041)."""
    market = rig.market
    with rig.factory() as session:
        rows = {
            r.instrument_id: r
            for r in session.scalars(
                select(HatWatchlistEntry).where(HatWatchlistEntry.trade_date == TRADE)
            )
        }
    assert len(rows) == 2
    s1, s2 = (rows[market.by_symbol(name).id] for name in (S1_NAME, S2_NAME))
    assert s1.playbook == "default"
    assert s1.status == lifecycle.EXPIRED
    assert s2.playbook == "breakout"
    assert s2.status == lifecycle.TRIGGERED


def _check_trades(rig: Rig) -> None:
    """Shadow entries become virtual positions. S2's trades through its stop at 10:58:35 and the
    live exits close it then (not the 15:20 close exits); the EP one stays open, its close-phase
    evaluation recorded
    once inside 15:20-15:25 (HAT-EXEC-074, HAT-OPS-034)."""
    market = rig.market
    with rig.factory() as session:
        assert _signal_of(session, S1_NAME, market) is None
        s2 = _signal_of(session, S2_NAME, market)
        ep = _signal_of(session, EP_NAME, market)
        positions = {p.signal_id: p for p in session.scalars(select(HatPosition))}
    assert s2 is not None
    assert ep is not None
    assert [s2.mode, ep.mode] == [SHADOW, SHADOW]
    assert ep.playbook == "episodic_pivot"
    assert s2.state == lifecycle.CLOSED
    assert ep.state == lifecycle.OPEN
    stopped, held = positions[s2.id], positions[ep.id]
    assert [stopped.is_virtual, held.is_virtual] == [True, True]
    assert stopped.closed_at is not None
    assert _local(stopped.closed_at) < (TRADE, STOPPED_BY)
    assert held.closed_at is None
    evaluated = [e for e in _events(rig, "exit.close_evaluated") if e.correlation_id == ep.id]
    assert len(evaluated) == 1
    day, at = _local(evaluated[0].occurred_at)
    assert day == TRADE
    assert CLOSE_FROM <= at < CLOSE_UNTIL


def _check_broker(rig: Rig) -> None:
    """Shadow mode: the adapter only ever read the account (reconciliation, fills, funds)."""
    called = {name for name, _ in rig.fake.calls}
    assert called - BROKER_READS == set()
    assert rig.fake.count("place_order") == 0


def _check_telegram(rig: Rig) -> None:
    """The login prompt before the open, the after-close reports, and each night's digest, one
    per bot; every recorded message went through the transport."""
    with rig.factory() as session:
        sent = [
            (_local(m.sent_at)[0], m.kind)
            for m in session.scalars(select(HatTelegramMessage).order_by(HatTelegramMessage.id))
        ]
    trade_kinds = {kind for day, kind in sent if day == TRADE}
    assert {"login_prompt", "rejections", "daily_report", WATCHLIST} <= trade_kinds
    assert "login_reprompt" not in trade_kinds  # the account's session was live by 09:05
    assert sent.count((NIGHT, WATCHLIST)) == len(rig.world.bots)
    assert sent.count((TRADE, WATCHLIST)) == len(rig.world.bots)
    assert len(rig.transport.texts()) >= len(sent)


def _check_stopped(rig: Rig) -> None:
    runtime = rig.runtime
    assert runtime.started is False
    assert runtime.scheduler is not None
    assert runtime.scheduler._task is None
    assert runtime._tasks == []
    assert rig.leftover == set()
    assert rig.feed.stopped == 1
    assert rig.feed.listeners == 0


def _rig(engine: Engine, tmp_path: Path) -> Rig:
    factory = sessionmaker(engine, expire_on_commit=False)
    clock = FixedClock(START)
    market = load_market()
    store = DataStore(tmp_path / "hat")
    seed_store(store, market)
    world = seed_world(factory, market, USER, store)
    feed = DayFeed()
    mcp = DayMcp(market, clock)
    _script_trade_day(market, mcp)
    fake = FakeBroker()
    transport = PolledTransport()
    urls: list[str] = []

    def parts() -> RuntimeParts:
        return RuntimeParts(
            store=store,
            feed=feed,
            telegram=telegram_settings(),
            transport=transport,
            broker_for=broker_for(market, fake),
            mcp=mcp.client,
            bhavcopy=lambda: bhavcopy_fetcher(market, clock, urls),
            sources=TextSources(
                equity_list=str,
                holidays=holiday_text,
                price_bands=lambda _day: lambda: band_text(market),
            ),
            sleep=lambda seconds: clock.advance(timedelta(seconds=seconds)),
        )

    runtime = HatRuntime(
        HatSettings(enabled=True, user_id=USER, data_root=tmp_path / "hat"),
        factory,
        dialect_name="postgresql",
        parts=parts,
        clock=clock,
    )
    return Rig(clock, factory, market, world, runtime, feed, mcp, fake, transport, urls)


def test_ops__simulated_day(clean_db: Engine, tmp_path: Path) -> None:
    """DB ops.simulated_day (10-ops §1-§3; HAT-OPS-003, 012, 021, 022, 023): a shadow trading
    day through `HatRuntime` with HAT enabled, on fixtures and fakes only."""
    rig = _rig(clean_db, tmp_path)
    asyncio.run(_simulate(rig))

    _check_night(rig, NIGHT, "mcp")
    _check_watchlist(rig)
    _check_trading_day_jobs(rig)
    _check_trades(rig)
    _check_night(rig, TRADE, "bhavcopy")
    assert _ingest_report(rig, TRADE)["probes"] == EXPECTED_PROBES
    assert len(rig.bhav_urls) == 1
    _check_holiday(rig)
    _check_broker(rig)
    _check_telegram(rig)
    _check_stopped(rig)
