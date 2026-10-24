"""The wiring of the daily cycle (audit R6): `build` turns the `RuntimeParts` into one frozen
`Assembly` (the desks, the live data, the trigger service, the job bodies, the Telegram service
and relay, the scheduler with the job table), and sets nothing on the runtime. `HatRuntime`
holds the assembly once it is built, or None.

Everything outside HAT's own code (the feed, the Telegram transport, the brokers, the NSE
sources) comes in through `RuntimeParts`, so the same assembly runs over fakes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
import threading
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import Clock
from ..data.bars_store import BarStore
from ..data.bhavcopy import Fetcher
from ..data.calendar import session_calendar
from ..data.instruments import symbol_ids
from ..data.live import EventFeedHealth, LiveData, silence_alert, threadsafe_provider
from ..data.mcp_client import McpClient
from ..data.quality import JOB_QUALITY
from ..data.staleness import SilenceWatch
from ..data.store import DataStore
from ..data.subscriptions import HAT_OWNER, LiveSubscriptions, SubscriptionRegistry
from ..engine.live_entry import DataHealthKill, TriggerService
from ..engine.portfolio.jobs import ALLOCATE_JOB, REGIME_JOB
from ..engine.strategy.config import StrategyConfig, parse_config
from ..engine.trigger import TriggerMonitor, locked_upper_circuit
from ..execution.broker import HatBroker
from ..execution.entry_timeout import EntryWatch
from ..execution.router import PlacementCheck
from ..models import HatBot, HatBrokerAccount, HatInstrument, HatSignal, HatStrategyVersion
from ..notify import Notifier
from ..ops.jobs import SessionFactory
from ..ops.scheduler import DEFAULT_DEADLINE, HatScheduler, ScheduledJob
from ..ops.trade_jobs import EXITS_CLOSE, GTT_CHECK, GTT_SWAP, GTT_VERIFY
from ..risk.sessions import SessionCalendar, is_trading_day
from ..settings import HatSettings
from ..telegram.callbacks import ManagerFlattener, RouterDesk
from ..telegram.client import BotClient, TelegramTransport
from ..telegram.config import TelegramSettings
from ..telegram.confirm import PendingConfirmations
from ..telegram.notifier import TelegramNotifier
from ..telegram.reads import LoginLinks
from ..telegram.relay import EventRelay
from ..telegram.reports import Reports, telegram_jobs
from ..telegram.service import TelegramService
from ..telegram.tokens import ApprovalSigner
from ..time import trade_date_of, utc_now
from .accounts import END_SESSION, Desks, account_jobs
from .entries import LiveContexts, LiveRoute
from .exits import ExitViews, ShadowExits
from .gates import GateBoard
from .intraday import (
    EXITS_CLOSE_SHADOW,
    GAPSCAN,
    LIVE_PREPARE,
    MODES_APPLY,
    WATCHLIST_EXPIRE,
    Intraday,
    intraday_jobs,
)
from .market import DailyBook
from .nightly import (
    BANDS_NIGHTLY,
    CA_ADJUST,
    CA_RESCALE,
    CALENDAR_REFRESH,
    CLOSE_GAPS,
    COVERAGE_CHECK,
    EVENTS_PARTITIONS,
    FEATURES,
    INGEST_NIGHTLY,
    JOURNAL_CHECK,
    REFERENCE_WEEKLY,
    RETENTION_PURGE,
    SCAN,
    SNAPSHOT_BOTS,
    WATCHLIST_DIGEST,
    NightlyDeps,
    TextSources,
    nightly_jobs,
)
from .periodic import Heartbeats
from .prices import LivePrices, PriceBook, SaleBands, stale_feed

__all__ = [
    "ALERT_POOL",
    "Assembly",
    "Feed",
    "RuntimeParts",
    "TradingCalendar",
    "build",
    "daily_jobs",
    "subscription_alert",
]

logger = logging.getLogger(__name__)


# one worker for the subscription alert: a sync runs on a job thread under the subscriptions'
# lock, and a notifier blocks on its transport
ALERT_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hat-alert")


def subscription_alert(notifier: Notifier, clock: Clock) -> Callable[[str], None]:
    """`LiveSubscriptions`' alert as a health alert of today's trade date (`subscription_limit`:
    the wanted set reached the provider's symbol limit, HAT-DATA-112; PR 73, PR 39), sent on
    `ALERT_POOL`, never on the caller's thread (the loop, or a job holding the subscriptions'
    lock). A failed send is logged by its error type."""

    def send(code: str, day: date) -> None:
        try:
            notifier.alert(code, day, {})
        except Exception as exc:  # the alert is best effort; the log keeps the type only
            logger.warning("subscription alert not sent: %s", type(exc).__name__)

    def alert(code: str) -> None:
        ALERT_POOL.submit(send, code, trade_date_of(clock.now()))

    return alert


class Feed(Protocol):
    """What the runtime needs of the live feed (`data.feed.LiveFeed`)."""

    registry: SubscriptionRegistry

    def add_tick_listener(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]: ...

    async def subscribe(self, symbols: list[str]) -> None: ...

    async def unsubscribe(self, symbols: list[str]) -> None: ...

    def start(
        self, loop: asyncio.AbstractEventLoop, *, prevalidated_token: bool | None = None
    ) -> str: ...

    async def stop(self) -> None: ...

    @property
    def is_connected(self) -> bool: ...


@dataclass
class RuntimeParts:
    """Everything outside HAT's code. Production builds them in `production_parts`; the
    simulated day passes fixtures, `FakeBroker` and a recording Telegram transport."""

    store: DataStore
    feed: Feed
    telegram: TelegramSettings
    transport: TelegramTransport
    broker_for: Callable[[HatBrokerAccount], HatBroker | None]
    mcp: Callable[[], McpClient | None]
    bhavcopy: Callable[[], Fetcher]
    sources: TextSources
    bar_store: BarStore | None = None
    announcements: Callable[[str], str] | None = None
    links: LoginLinks | None = None
    sleep: Callable[[float], None] | None = None  # the ingest's wait; default: wall clock
    prevalidated_token: bool | None = None


# How long after its time (`at`) each job's run has to have succeeded before the owner is told
# (P5, `ops/scheduler.py:overdue`), by the job's name without its account. The one table of
# deadlines; a job not in it gets `DEFAULT_DEADLINE`. Each is set by what waits on the job.
_NIGHTLY_DONE = timedelta(hours=6, minutes=30)  # the chain from 18:00 by 00:30
JOB_DEADLINES: Mapping[str, timedelta] = {
    INGEST_NIGHTLY: timedelta(hours=4, minutes=30),  # its wait ends at 22:00
    CA_ADJUST: _NIGHTLY_DONE,
    JOB_QUALITY: _NIGHTLY_DONE,
    FEATURES: _NIGHTLY_DONE,
    REGIME_JOB: _NIGHTLY_DONE,
    ALLOCATE_JOB: _NIGHTLY_DONE,
    SCAN: _NIGHTLY_DONE,
    WATCHLIST_DIGEST: _NIGHTLY_DONE,
    CLOSE_GAPS: _NIGHTLY_DONE,
    COVERAGE_CHECK: _NIGHTLY_DONE,
    SNAPSHOT_BOTS: timedelta(hours=1),  # 23:30: the drawdown kills of the day
    JOURNAL_CHECK: timedelta(hours=1),
    GTT_VERIFY: timedelta(hours=1),  # 23:00
    CALENDAR_REFRESH: timedelta(hours=1),
    BANDS_NIGHTLY: timedelta(hours=1),  # 07:45: the bands before the pre-open
    CA_RESCALE: timedelta(minutes=20),  # 08:40: before the 09:00 pre-open
    GTT_CHECK: timedelta(minutes=20),  # 08:40: before the open
    LIVE_PREPARE: timedelta(minutes=15),  # 08:45: armed by the pre-open
    MODES_APPLY: timedelta(minutes=10),
    GAPSCAN: timedelta(minutes=10),  # window 09:16-09:20
    EXITS_CLOSE: timedelta(minutes=10),  # window 15:20-15:25
    EXITS_CLOSE_SHADOW: timedelta(minutes=10),
    GTT_SWAP: timedelta(minutes=15),  # 15:25: the overnight stops are GTTs
    WATCHLIST_EXPIRE: timedelta(minutes=30),
    END_SESSION: timedelta(minutes=30),
    REFERENCE_WEEKLY: timedelta(hours=2),
    EVENTS_PARTITIONS: timedelta(hours=1),
    RETENTION_PURGE: timedelta(hours=2),
}


def with_deadlines(
    jobs: tuple[ScheduledJob, ...], deadlines: Mapping[str, timedelta] = JOB_DEADLINES
) -> tuple[ScheduledJob, ...]:
    """`jobs` with their deadline from `deadlines`, by the name before any `:account`."""
    return tuple(
        dataclasses.replace(
            job, deadline=deadlines.get(job.name.split(":", 1)[0], DEFAULT_DEADLINE)
        )
        for job in jobs
    )


def daily_jobs(
    nightly: NightlyDeps,
    intraday: Intraday,
    desks: Desks,
    views: ExitViews,
    reports: Reports,
) -> tuple[ScheduledJob, ...]:
    """Every job of the daily cycle (the job table, `CONF ops.job_registry`), each with its
    deadline."""
    return with_deadlines(
        (
            *nightly_jobs(nightly),
            *intraday_jobs(intraday),
            *account_jobs(desks, nightly.session_factory, views, nightly.clock),
            *telegram_jobs(reports),
        )
    )


def _configs(session: Session) -> list[StrategyConfig]:
    versions = session.scalars(
        select(HatStrategyVersion)
        .join(HatBot, HatBot.live_version_id == HatStrategyVersion.id)
        .where(HatBot.status == "active")
    )
    return [parse_config(v.config) for v in versions]


def _instrument_ids(session_factory: SessionFactory) -> Mapping[str, uuid.UUID]:
    with session_factory() as session:
        rows = session.execute(select(HatInstrument.id, HatInstrument.symbol, HatInstrument.active))
        return symbol_ids(rows)


class TradingCalendar:
    """The trading calendar, cached: read at start and again by the periodic calendar check
    (off the event loop); every other reader takes the cached one."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._calendar: SessionCalendar | None = None

    def __call__(self) -> SessionCalendar | None:
        return self._calendar

    def read(self) -> SessionCalendar | None:
        with self._session_factory() as session:
            self._calendar = session_calendar(session)
        return self._calendar

    def trading(self, day: date) -> bool:
        return is_trading_day(self._calendar, day)

    def is_open(self, at: datetime) -> bool:
        calendar = self._calendar
        return calendar is not None and calendar.is_open(at)


@dataclass(frozen=True)
class TakeInputs:
    """A Take's live inputs (`RouterDesk`): the bot's placement check and, at its checked live
    price (PR 51, as at placement), whether the instrument is locked at its upper circuit."""

    route: LiveRoute
    book: DailyBook

    def __call__(
        self, session: Session, signal: HatSignal, now: datetime
    ) -> tuple[Decimal | None, bool | None, PlacementCheck]:
        check = self.route.check(session, signal.bot_id, signal.instrument_id, now)
        price, locked = check.ltp, None
        if price is not None:
            daily = self.book.day(trade_date_of(now)).daily(session, signal.instrument_id)
            locked = locked_upper_circuit(price, daily)
        return price, locked, check


@dataclass(frozen=True)
class Assembly:
    """The daily cycle, wired once at start."""

    parts: RuntimeParts
    session_factory: SessionFactory
    user_id: uuid.UUID
    calendar: TradingCalendar
    notifier: Notifier
    book: DailyBook
    prices: PriceBook
    live: LiveData
    desks: Desks
    route: LiveRoute
    service: TriggerService
    entries: EntryWatch
    views: ExitViews
    shadow: ShadowExits
    subscriptions: LiveSubscriptions
    intraday: Intraday
    reports: Reports
    telegram: TelegramService
    relay: EventRelay
    jobs: tuple[ScheduledJob, ...]
    heartbeats: Heartbeats
    scheduler: HatScheduler
    feed_stale: Callable[[], bool]
    lock_lost: threading.Event  # set by `HatRuntime.watch_lock`: no new entry until a restart


def build(
    parts: RuntimeParts,
    settings: HatSettings,
    session_factory: SessionFactory,
    clock: Clock,
    loop: asyncio.AbstractEventLoop,
    calendar: TradingCalendar,
    *,
    sleep: Callable[[float], None],
) -> Assembly:
    """Wire the daily cycle over `parts` (blocking: the desks read the accounts and build the
    brokers). `sleep` is the ingest's wait when the parts give none."""
    sf = session_factory
    client = BotClient(parts.transport, parts.telegram.chat_id)
    signer = ApprovalSigner(parts.telegram.approval_secret)
    notifier = TelegramNotifier(client, sf)
    book = DailyBook(parts.store, sf, _configs)
    prices = PriceBook()
    gates = GateBoard(parts.store)
    subscribe, unsubscribe = threadsafe_provider(parts.feed, loop)
    registry = parts.feed.registry
    live = LiveData(
        health=EventFeedHealth(sf, DataHealthKill(sf, clock)),
        is_open=calendar.is_open,
        clock=clock,
        store=parts.bar_store,
        instrument_ids=functools.partial(_instrument_ids, sf),
        exchange_range=settings.exchange_day_range,
        silence=SilenceWatch(
            calendar.is_open,
            silence_alert(notifier),
            subscribe,
            silent_after=timedelta(seconds=settings.symbol_silent_seconds),
        ),
        watched=lambda: registry.symbols_of(HAT_OWNER),
    )
    live.add_tick_listener(prices.on_tick)
    desks = Desks(
        sf,
        parts.broker_for,
        notifier,
        issuer=signer.issue,
        calendar=calendar,
        tick_for=lambda _s, p: book.day(trade_date_of(utc_now(clock))).tick(p.instrument_id),
        clock=clock,
        band=SaleBands(prices, book),
        resend=settings.mpp_resend,
        page_after=settings.mpp_page_after,
        gtt_check_every=timedelta(minutes=settings.gtt_check_minutes),
    )

    # the runtime lost its lock (`HatRuntime.watch_lock`): another runtime may run, so every
    # entry path (trigger, placement, approval take, resume) reads it as a stale feed and
    # refuses; stops and exits never read it
    lock_lost = threading.Event()

    def feed_stale() -> bool:
        return lock_lost.is_set() or stale_feed(live.staleness, utc_now(clock))

    def feed_state() -> str:
        if stale_feed(live.staleness, utc_now(clock)):
            return "stale"
        return "connected" if parts.feed.is_connected else "disconnected"

    # an exit view takes an illiquid name's last trade while the feed itself is fresh (QA15)
    live_prices = LivePrices(
        prices, book, clock, lambda: not stale_feed(live.staleness, utc_now(clock))
    )
    # PR 49: an unfilled entry is cancelled on a failed break or after the timeout; a failed
    # break also refuses its placement and a Take (Q7), from the closes a restart restores
    entries = EntryWatch(timedelta(minutes=settings.entry_timeout_minutes))
    live.add_restore_listener(entries.restore)
    route = LiveRoute(desks.router, gates, feed_stale, clock, calendar, live_prices, entries.closes)
    contexts = LiveContexts(gates, settings.user_id, feed_stale, desks.funds, calendar)
    service = TriggerService(TriggerMonitor(live.builder), sf, contexts, feed_stale, route)
    live.add_bar_listener(service.on_bar)
    live.add_bar_listener(entries.on_bar)
    views = ExitViews(book, live_prices.exit_price, clock, calendar, sf, notifier)
    shadow = ShadowExits(sf, views, desks.managed)
    subscriptions = LiveSubscriptions(
        registry, subscribe, unsubscribe, subscription_alert(notifier, clock)
    )
    intraday = Intraday(
        session_factory=sf,
        clock=clock,
        book=book,
        prices=prices,
        live=live,
        service=service,
        subscriptions=subscriptions,
        shadow=shadow,
        notifier=notifier,
        gap_client=parts.mcp,
        calendar=calendar,
    )
    reports = Reports(client, sf, links=parts.links, clock=clock)
    telegram = TelegramService(
        client,
        sf,
        signer=signer,
        confirmations=PendingConfirmations(),
        desk=RouterDesk(desks.router, TakeInputs(route, book)),
        flattener=ManagerFlattener(desks.managers),
        links=parts.links,
        price=prices.price,
        feed=feed_state,
        clock=clock,
    )
    relay = EventRelay(client, sf, signer, clock)
    nightly = NightlyDeps(
        store=parts.store,
        session_factory=sf,
        clock=clock,
        user_id=settings.user_id,
        mcp=parts.mcp,
        bhavcopy=parts.bhavcopy,
        sources=parts.sources,
        total_equity=desks.total_equity,
        fills_read_at=desks.fills_read_at,
        sleep=parts.sleep or sleep,
        notifier=notifier,
        digest=reports.send_watchlist_digest,
        on_loaded=book.reset,
    )
    jobs = daily_jobs(nightly, intraday, desks, views, reports)
    heartbeats = Heartbeats(notifier, calendar.is_open)
    scheduler = HatScheduler(
        jobs, sf, clock, trading=calendar.trading, notifier=notifier, watch=heartbeats.check
    )
    return Assembly(
        parts=parts,
        session_factory=sf,
        user_id=settings.user_id,
        calendar=calendar,
        notifier=notifier,
        book=book,
        prices=prices,
        live=live,
        desks=desks,
        route=route,
        service=service,
        entries=entries,
        views=views,
        shadow=shadow,
        subscriptions=subscriptions,
        intraday=intraday,
        reports=reports,
        telegram=telegram,
        relay=relay,
        jobs=jobs,
        heartbeats=heartbeats,
        scheduler=scheduler,
        feed_stale=feed_stale,
        lock_lost=lock_lost,
    )
