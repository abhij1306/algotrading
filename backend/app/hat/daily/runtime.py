"""The HAT runtime the lifespan starts when `HAT_ENABLED` is true (PR 17).

With HAT disabled `HatRuntime.start` starts nothing (HAT-OPS-001). Enabled, it refuses a database
that is not PostgreSQL (HAT-OPS-002) and a bad Telegram configuration (HAT-TG-040) before any
task exists, then starts in the restart-safe order of HAT-OPS-003:

1. reap the job runs a previous process left running, and read the trading calendar;
2. build one desk per active broker account and reconcile each before anything is placed, read
   its fills (a fill without a working stop is protected) and note the auto entries left in
   Triggered: the periodic loop places them once the feed is fresh (no tick since the start
   reads as stale) and the account has reconciled;
3. on a trading day from 08:45, arm the day (watchlist candidates, subscriptions, keys already
   fired);
4. attach the live data to the feed and start the feed;
5. start the scheduler with every job of the daily cycle, the periodic loop (session watch, fill
   sync, approval expiry, live exits, reconciliation, announcements) and the Telegram poller and
   relay.

`stop` ends them in reverse, each step even if another failed. Everything outside HAT's own code
(the feed, the Telegram transport, the brokers, the NSE sources) comes in through `RuntimeParts`,
so the simulated day runs the same assembly over fakes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import SYSTEM_CLOCK, Clock
from ..data.announcements import INTRADAY_FEEDS, poll_all, poll_interval
from ..data.bars_store import BarStore
from ..data.bhavcopy import Fetcher
from ..data.calendar import session_calendar
from ..data.live import EventFeedHealth, LiveData, silence_alert, threadsafe_provider
from ..data.mcp_client import McpClient
from ..data.staleness import SilenceWatch
from ..data.store import DataStore
from ..data.subscriptions import HAT_OWNER, LiveSubscriptions, SubscriptionRegistry
from ..engine.live_entry import DataHealthKill, TriggerService
from ..engine.strategy.config import StrategyConfig, parse_config
from ..engine.trigger import TriggerMonitor, locked_upper_circuit
from ..execution.broker import HatBroker
from ..execution.entry_timeout import EntryWatch
from ..execution.router import PlacementCheck
from ..logs import configure_hat_logging
from ..models import (
    HatBot,
    HatBrokerAccount,
    HatInstrument,
    HatSignal,
    HatStrategyVersion,
)
from ..notify import Notifier
from ..ops.jobs import JobError, SessionFactory, recover_interrupted
from ..ops.scheduler import HatScheduler, ScheduledJob
from ..risk.sessions import SessionCalendar, is_trading_day
from ..settings import HatSettings, require_postgres
from ..telegram.callbacks import ManagerFlattener, RouterDesk
from ..telegram.client import BotClient, TelegramTransport
from ..telegram.config import TelegramSettings
from ..telegram.confirm import PendingConfirmations
from ..telegram.notifier import TelegramNotifier
from ..telegram.reads import LoginLinks
from ..telegram.relay import EventRelay
from ..telegram.reports import Reports, telegram_jobs
from ..telegram.service import TelegramService, run_relay
from ..telegram.tokens import ApprovalSigner
from ..time import IST, trade_date_of, utc_now
from .accounts import Desks, account_jobs
from .intraday import PREPARE_AT, Intraday, intraday_jobs
from .live import (
    ExitViews,
    GateBoard,
    LiveContexts,
    LivePrices,
    LiveRoute,
    PriceBook,
    SaleBands,
    ShadowExits,
    stale_feed,
)
from .market import DailyBook
from .nightly import NightlyDeps, TextSources, nightly_jobs

__all__ = [
    "ALERT_POOL",
    "STEP_SECONDS",
    "HatRuntime",
    "RuntimeParts",
    "daily_jobs",
    "subscription_alert",
]

logger = logging.getLogger(__name__)

STEP_SECONDS = 5.0  # the periodic loop: live exits on the latest prices
MINUTE = timedelta(minutes=1)
RECONCILE_EVERY = timedelta(minutes=5)
CALENDAR_EVERY = timedelta(minutes=10)
STOPPING = "hat_stopping"


# one worker for the subscription alert: a sync runs on a job thread under the subscriptions'
# lock, and a notifier blocks on its transport
ALERT_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hat-alert")


def subscription_alert(notifier: Notifier, now: Callable[[], datetime]) -> Callable[[str], None]:
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
        ALERT_POOL.submit(send, code, trade_date_of(now()))

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


def daily_jobs(
    nightly: NightlyDeps,
    intraday: Intraday,
    desks: Desks,
    views: ExitViews,
    reports: Reports,
    *,
    exdate_check: bool = False,
) -> tuple[ScheduledJob, ...]:
    """Every job of the daily cycle (the job table, `CONF ops.job_registry`); the
    flagged `gtt.ex_date` only with `exdate_check` (`HAT_EXDATE_GTT_CHECK`, PR 47)."""
    return (
        *nightly_jobs(nightly),
        *intraday_jobs(intraday),
        *account_jobs(
            desks, nightly.session_factory, views, nightly.clock, exdate_check=exdate_check
        ),
        *telegram_jobs(reports),
    )


def _configs(session: Session) -> list[StrategyConfig]:
    versions = session.scalars(
        select(HatStrategyVersion)
        .join(HatBot, HatBot.live_version_id == HatStrategyVersion.id)
        .where(HatBot.status == "active")
    )
    return [parse_config(v.config) for v in versions]


class HatRuntime:
    def __init__(
        self,
        settings: HatSettings,
        session_factory: SessionFactory,
        *,
        dialect_name: str,
        parts: Callable[[], RuntimeParts],
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.settings = settings
        self._session_factory = session_factory
        self._dialect_name = dialect_name
        self._parts = parts
        self.clock = clock
        self._halt = threading.Event()  # long job bodies (the ingest's wait) end on it
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._calendar: SessionCalendar | None = None
        self._last: dict[str, datetime] = {}
        self.started = False
        self.scheduler: HatScheduler | None = None

    # --- reading --------------------------------------------------------------------------

    def _now(self) -> datetime:
        return utc_now(self.clock)

    def calendar(self) -> SessionCalendar | None:
        """The trading calendar, cached (read again by the periodic loop off the event loop)."""
        return self._calendar

    def _read_calendar(self) -> SessionCalendar | None:
        with self._session_factory() as session:
            self._calendar = session_calendar(session)
        desks = getattr(self, "desks", None)
        if desks is not None:
            desks.use_calendar(self._calendar)
        return self._calendar

    def trading(self, day: date) -> bool:
        return is_trading_day(self._calendar, day)

    def is_open(self, at: datetime) -> bool:
        calendar = self._calendar
        return calendar is not None and calendar.is_open(at)

    def _sleep(self, seconds: float) -> None:
        """The ingest's wait: ends with a `JobError` as soon as HAT stops."""
        if self._halt.wait(seconds):
            raise JobError(STOPPING)

    # --- start ----------------------------------------------------------------------------

    async def start(self) -> bool:
        """Start HAT; False (and nothing started) when HAT is disabled. Raises `HatConfigError`
        for a non-Postgres database, a proxy that would carry Fyers traffic (`fyers_proxy_set`)
        or a bad Telegram configuration, before any task exists."""
        if not self.settings.enabled:
            logger.info("HAT_ENABLED is false: no HAT task is started")
            return False
        require_postgres(self._dialect_name)
        # reads the Telegram settings (a bad one refuses the start) and builds the HTTP clients
        parts = await asyncio.to_thread(self._parts)
        configure_hat_logging()
        reaped = await asyncio.to_thread(recover_interrupted, self._session_factory)
        if reaped:
            logger.warning("recovered %d interrupted job runs", reaped)
        await asyncio.to_thread(self._read_calendar)
        # the desks read the accounts and build the brokers: blocking, so off the loop
        await asyncio.to_thread(self._build, parts, asyncio.get_running_loop())
        await self._recover()
        self._start_live(parts)
        self._start_tasks()
        self.started = True
        logger.info("HAT runtime started")
        return True

    def _build(self, parts: RuntimeParts, loop: asyncio.AbstractEventLoop) -> None:
        sf = self._session_factory
        clock = self.clock
        self.parts = parts
        self.client = BotClient(parts.transport, parts.telegram.chat_id)
        self.signer = ApprovalSigner(parts.telegram.approval_secret)
        self.notifier = TelegramNotifier(self.client, sf)
        self.book = DailyBook(parts.store, sf, _configs)
        self.prices = PriceBook()
        self.gates = GateBoard(parts.store)
        subscribe, unsubscribe = threadsafe_provider(parts.feed, loop)
        registry = parts.feed.registry
        self.live = LiveData(
            health=EventFeedHealth(sf, DataHealthKill(sf, lambda: utc_now(clock))),
            is_open=self.is_open,
            clock=clock,
            store=parts.bar_store,
            instrument_ids=self._instrument_ids,
            exchange_range=self.settings.exchange_day_range,
            silence=SilenceWatch(
                self.is_open,
                silence_alert(self.notifier),
                subscribe,
                silent_after=timedelta(seconds=self.settings.symbol_silent_seconds),
            ),
            watched=lambda: registry.symbols_of(HAT_OWNER),
        )
        self.live.add_tick_listener(self.prices.on_tick)
        self.desks = Desks(
            sf,
            parts.broker_for,
            self.notifier,
            issuer=self.signer.issue,
            calendar=self.calendar,
            tick_for=lambda _s, p: self.book.day(trade_date_of(self._now())).tick(p.instrument_id),
            today=lambda: trade_date_of(self._now()),
            band=SaleBands(self.prices, self.book),
            resend=self.settings.mpp_resend,
            page_after=self.settings.mpp_page_after,
        )
        feed_stale = self._feed_stale
        self.live_prices = LivePrices(self.prices, self.book, clock)
        self.route = LiveRoute(
            self.desks.router, self.gates, feed_stale, clock, self.calendar, self.live_prices
        )
        contexts = LiveContexts(
            self.gates, self.settings.user_id, feed_stale, self._funds, self.calendar
        )
        self.service = TriggerService(
            TriggerMonitor(self.live.builder), sf, contexts, feed_stale, self.route
        )
        self.live.add_bar_listener(self.service.on_bar)
        # PR 49: an unfilled entry is cancelled on a failed break or after the timeout
        self.entries = EntryWatch(timedelta(minutes=self.settings.entry_timeout_minutes))
        self.live.add_bar_listener(self.entries.on_bar)
        self.views = ExitViews(self.book, self.live_prices.price, clock, self.calendar)
        self.shadow = ShadowExits(sf, self.views, self.desks.managed)
        self.subscriptions = LiveSubscriptions(
            registry, subscribe, unsubscribe, subscription_alert(self.notifier, self._now)
        )
        self.intraday = Intraday(
            session_factory=sf,
            clock=clock,
            book=self.book,
            prices=self.prices,
            live=self.live,
            service=self.service,
            subscriptions=self.subscriptions,
            shadow=self.shadow,
            notifier=self.notifier,
            gap_client=parts.mcp,
            calendar=self.calendar,
        )
        self.reports = Reports(self.client, sf, links=parts.links, clock=self._now)
        self.telegram = TelegramService(
            self.client,
            sf,
            signer=self.signer,
            confirmations=PendingConfirmations(),
            desk=RouterDesk(self.desks.router, self._take_inputs),
            flattener=ManagerFlattener(self.desks.managers),
            links=parts.links,
            price=self.prices.price,
            feed=self._feed_state,
            clock=self._now,
        )
        self.relay = EventRelay(self.client, sf, self.signer, self._now)
        nightly = NightlyDeps(
            store=parts.store,
            session_factory=sf,
            clock=clock,
            user_id=self.settings.user_id,
            mcp=parts.mcp,
            bhavcopy=parts.bhavcopy,
            sources=parts.sources,
            total_equity=self._total_equity,
            sleep=parts.sleep or self._sleep,
            notifier=self.notifier,
            digest=self.reports.send_watchlist_digest,
            on_loaded=self.book.reset,
        )
        self.jobs = daily_jobs(
            nightly,
            self.intraday,
            self.desks,
            self.views,
            self.reports,
            exdate_check=self.settings.exdate_gtt_check,
        )
        self.scheduler = HatScheduler(self.jobs, sf, self._now, trading=self.trading)

    async def _recover(self) -> None:
        """HAT-OPS-003: reconcile and read fills (the entries left in Triggered wait for a fresh
        feed: `resume_entries`), then arm a day already under way (a restart after 08:45 on a
        trading day)."""
        now = self._now()
        await asyncio.to_thread(self._watch_sessions, now)
        await asyncio.to_thread(self.desks.recover, now)
        today = trade_date_of(now)
        if self.trading(today) and now.astimezone(IST).time() >= PREPARE_AT:
            try:
                # before the feed starts (the bar builder is the loop thread's once it does): the
                # bars stored before the restart give back the opening ranges and day volumes
                await asyncio.to_thread(self.live.restore, today)
                await asyncio.to_thread(self.intraday.prepare, today)
            except Exception:  # the day stays unarmed (no entries); exits and stops still run
                logger.exception("arming the day on restart failed")

    def _start_live(self, parts: RuntimeParts) -> None:
        self.live.attach(parts.feed)
        parts.feed.start(asyncio.get_running_loop(), prevalidated_token=parts.prevalidated_token)

    def _start_tasks(self) -> None:
        assert self.scheduler is not None
        self.scheduler.start()
        loop = asyncio.get_running_loop()
        for work in (
            self.live.run(),
            self._periodic_loop(),
            self.telegram.run(self._stop),
            run_relay(self.relay, self._stop),
        ):
            self._tasks.append(loop.create_task(work))

    # --- the periodic steps ---------------------------------------------------------------

    def _due(self, name: str, now: datetime, every: timedelta) -> bool:
        last = self._last.get(name)
        if last is not None and now - last < every:
            return False
        self._last[name] = now
        return True

    def periodic(self, now: datetime) -> None:
        """One pass of the periodic work (blocking: runs off the loop). Each part fails alone."""
        steps: list[tuple[str, timedelta, Callable[[], object]]] = [
            ("calendar", CALENDAR_EVERY, self._read_calendar),
        ]
        if self.trading(trade_date_of(now)):
            steps += [
                ("sessions", MINUTE, lambda: self._watch_sessions(now)),
                ("sync", timedelta(0), lambda: self.desks.sync_due(now)),
                ("expire", MINUTE, lambda: self.desks.expire_all(now)),
                ("reconcile", RECONCILE_EVERY, lambda: self.desks.reconcile_all(now)),
                (
                    "resume",
                    timedelta(0),
                    lambda: self.desks.resume_entries(now, self.route.check, self._feed_stale),
                ),
            ]
            if self.is_open(now):
                steps.append(("exits", timedelta(0), lambda: self._live_exits(now)))
                steps.append(
                    (
                        "entries",
                        timedelta(0),
                        lambda: self.desks.cancel_stale_entries(now, self.entries),
                    )
                )
        if self.parts.announcements is not None:
            steps.append(("announcements", poll_interval(now), lambda: self._poll(now)))
        for name, every, step in steps:
            if not self._due(name, now, every):
                continue
            try:
                step()
            except Exception:
                logger.exception("periodic step %s failed", name)

    def _watch_sessions(self, now: datetime) -> None:
        if self.trading(trade_date_of(now)):
            self.desks.watch_sessions(now)

    def _poll(self, now: datetime) -> None:
        fetch = self.parts.announcements
        if fetch is not None:
            poll_all(INTRADAY_FEEDS, fetch, self._session_factory, now=now)

    def _live_exits(self, now: datetime) -> int:
        """The live exit rules: every virtual position, then each account's real positions
        through its `TradeManager` (stops follow, sales go out)."""
        done = self.shadow.run(now)
        for account_id, manager in self.desks.managers():
            if not manager.orders_allowed(now):
                continue
            with self._session_factory() as session:
                for position in manager.open_positions(session, account_id):
                    view = self.views(session, position)
                    if view is None:
                        continue
                    try:
                        manager.on_price(session, position.id, view, now)
                        done += 1
                    except Exception:
                        session.rollback()
                        logger.exception("live exits of position %s failed", position.id)
        return done

    async def _periodic_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.periodic, self._now())
            except Exception:
                logger.exception("periodic pass failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), STEP_SECONDS)

    # --- inputs ---------------------------------------------------------------------------

    def _feed_stale(self) -> bool:
        return stale_feed(self.live.staleness, self._now())

    def _feed_state(self) -> str:
        if self._feed_stale():
            return "stale"
        return "connected" if self.parts.feed.is_connected else "disconnected"

    def _instrument_ids(self) -> Mapping[str, uuid.UUID]:
        with self._session_factory() as session:
            return {
                s: i for i, s in session.execute(select(HatInstrument.id, HatInstrument.symbol))
            }

    def _funds(self, account_id: uuid.UUID) -> Decimal | None:
        desk = self.desks.by_account.get(account_id)
        if desk is None:
            return None
        try:
            return desk.broker.funds().available
        except Exception:
            logger.warning("broker funds unreadable", extra={"account_id": str(account_id)})
            return None

    def _total_equity(self, _day: date) -> Decimal:
        """[OWNER-PENDING] HAT-PORT-052: the broker funds of every active account, read when the
        allocation runs. Fails the job when there is no account or one cannot be read."""
        desks = list(self.desks.by_account.values())
        if not desks:
            raise JobError("equity_unavailable")
        total = Decimal(0)
        for desk in desks:
            try:
                total += desk.broker.funds().total
            except Exception as exc:
                raise JobError("equity_unavailable") from exc
        return total

    def _take_inputs(
        self, session: Session, signal: HatSignal, now: datetime
    ) -> tuple[Decimal | None, bool | None, PlacementCheck]:
        check = self.route.check(session, signal.bot_id, signal.instrument_id, now)
        price, locked = check.ltp, None  # the checked live price (PR 51), as at placement
        if price is not None:
            daily = self.book.day(trade_date_of(now)).daily(session, signal.instrument_id)
            locked = locked_upper_circuit(price, daily)
        return price, locked, check

    # --- stop -----------------------------------------------------------------------------

    async def stop(self) -> None:
        """Stop in reverse order; each step runs even if an earlier one failed."""
        self._halt.set()
        self._stop.set()
        steps: list[tuple[str, Callable[[], Awaitable[None]]]] = [
            ("tasks", self._stop_tasks),
            ("scheduler", self._stop_scheduler),
            ("feed", self._stop_feed),
            ("desks", self._release),
        ]
        for name, step in steps:
            try:
                await step()
            except Exception as exc:
                logger.warning("HAT stop step %s failed: %s", name, type(exc).__name__)
        self.started = False

    async def _stop_tasks(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _stop_scheduler(self) -> None:
        if self.scheduler is not None:
            await self.scheduler.stop()

    async def _stop_feed(self) -> None:
        """Detach, release the subscriptions and stop the feed; after a start that failed part
        way each step runs on what was built, and the feed is always stopped."""
        parts = getattr(self, "parts", None)
        if parts is None:
            return
        for name, step in (("live", "detach"), ("subscriptions", "release_all")):
            built = getattr(self, name, None)
            if built is None:
                continue
            try:
                getattr(built, step)()
            except Exception as exc:
                logger.warning("HAT stop step %s failed: %s", name, type(exc).__name__)
        await parts.feed.stop()

    async def _release(self) -> None:
        if hasattr(self, "desks"):
            await asyncio.to_thread(self.desks.release)  # closes broker sessions: blocking
