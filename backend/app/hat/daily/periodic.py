"""The periodic work beside the scheduled jobs (audit R5), in independent loops so that a slow
step of one never delays another:

- `exits`, on a trading day while the market is open: the live exits (`live_exits`: each
  account's real positions through its `TradeManager`, then every virtual position as a step of
  its own, so a shadow failure never holds up a real exit), the drawdown
  kills on equity marked to market (every minute) and the stale-entry cancels. They read prices,
  the database and the managers;
- `housekeeping`: the calendar check, the runtime lock check (`HatRuntime.watch_lock`), the retry of
  a feed-health kill write that failed, and on a trading day the session watch, the fill sync,
  the approval expiry, the reconciliation (every 5 minutes), the resume of stranded entries and
  the tracking of positions opened after the day was armed (`Intraday.track_positions`) and the
  refresh of the EP candidates' results filings (`Intraday.refresh_results`);
- `announcements`, when the parts give a fetch: the NSE announcements poll (up to two feeds of
  `FETCH_TIMEOUT_SECONDS` each).

Each loop runs one pass on its own thread (never the loop's default pool, which the Telegram poll
and the relay share: audit A3 F6), then waits `STEP_SECONDS`; each step fails alone. Once the
runtime is halted (it lost its lock to another runtime, `Assembly.halt`) the exits pass and the
trading-day housekeeping steps do nothing: the runtime holding the lock owns the accounts.
Every loop beats a heartbeat at the start of each pass, and the scheduler's tick calls
`Heartbeats.check`: a loop whose heartbeat is older than `STALL_AFTER` while the market is open
raises `periodic.stalled` once per session (a pass of `announcements` takes at most two fetches of
`FETCH_TIMEOUT_SECONDS`, inside it). A wedged step is bounded, not killed: every broker call has
the broker guard's timeout and every database statement HAT's `statement_timeout`
(`app.database.HAT_STATEMENT_TIMEOUT_MS`), so the step fails and the loop beats again.

No step needs another loop's step to have run first. An entry fill the sync has not read yet has
no open position for the exits to act on; a sale reads the position again under its write lock
(`PositionDesk`, shared with the fill sync's stop writes) and leaves the hard stop to a stop the
broker confirmed resting; and every broker call goes through the account's one session owner.
`exits.close` and the Telegram flatten already ran beside the fill sync on those same terms.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable, Coroutine, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from ..clock import Clock
from ..data.announcements import INTRADAY_FEEDS, poll_all, poll_interval
from ..notify import Notifier
from ..risk.sessions import calendar_short
from ..time import IST, trade_date_of, utc_now

if TYPE_CHECKING:
    from .assembly import Assembly

__all__ = [
    "PERIODIC_STALLED",
    "STALL_AFTER",
    "STEP_SECONDS",
    "Heartbeats",
    "Periodic",
    "stalled",
    "watch_sessions",
]

logger = logging.getLogger(__name__)

STEP_SECONDS = 5.0  # the wait after each pass: live exits on the latest prices
STALL_AFTER = timedelta(seconds=60)
MINUTE = timedelta(minutes=1)
RECONCILE_EVERY = timedelta(minutes=5)
CALENDAR_EVERY = timedelta(minutes=10)
DRAWDOWN_EVERY = MINUTE  # the drawdown kills on equity marked to market (P5, X9)
ALERT_CALENDAR_SHORT = "calendar.short"
PERIODIC_STALLED = "periodic.stalled"
EXITS = "exits"
HOUSEKEEPING = "housekeeping"
ANNOUNCEMENTS = "announcements"

Step = tuple[str, timedelta, Callable[[], object]]


def stalled(
    beats: Mapping[str, datetime], now: datetime, after: timedelta = STALL_AFTER
) -> list[str]:
    """The loops whose last heartbeat is more than `after` before `now`, by name."""
    return sorted(name for name, at in beats.items() if now - at > after)


class Heartbeats:
    """The last pass start of each periodic loop, and the watchdog over them."""

    def __init__(self, notifier: Notifier, is_open: Callable[[datetime], bool]) -> None:
        self._notifier = notifier
        self._is_open = is_open
        self._lock = threading.Lock()  # beats come from the loops, checks from the scheduler
        self._beats: dict[str, datetime] = {}
        self._alerted: set[str] = set()

    def beat(self, name: str, at: datetime) -> None:
        with self._lock:
            self._beats[name] = at

    def check(self, now: datetime) -> list[str]:
        """Raise `periodic.stalled` for each loop stalled at `now` while the market is open, once
        until it beats in time again or the session ends; return the loops stalled now."""
        if not self._is_open(now):
            with self._lock:
                self._alerted.clear()
            return []
        with self._lock:
            beats = dict(self._beats)
            late = stalled(beats, now)
            fresh = [name for name in late if name not in self._alerted]
            self._alerted = set(late)
        for name in fresh:
            since = beats[name].astimezone(IST).strftime("%H:%M:%S")
            logger.error("periodic loop %s stalled since %s IST", name, since)
            delivered = False
            try:
                delivered = self._notifier.alert(
                    PERIODIC_STALLED, trade_date_of(now), {"loop": name, "since": since}
                )
            except Exception:
                logger.exception("alert %s for loop %s not sent", PERIODIC_STALLED, name)
            if not delivered:  # told again at the next check
                with self._lock:
                    self._alerted.discard(name)
        return late


class Every:
    """Each named step at most once per its interval."""

    def __init__(self) -> None:
        self._last: dict[str, datetime] = {}

    def due(self, name: str, now: datetime, every: timedelta) -> bool:
        last = self._last.get(name)
        if last is not None and now - last < every:
            return False
        self._last[name] = now
        return True


def _run_steps(steps: Sequence[Step], every: Every, now: datetime) -> None:
    for name, interval, step in steps:
        if not every.due(name, now, interval):
            continue
        try:
            step()
        except Exception:
            logger.exception("periodic step %s failed", name)


def watch_sessions(assembly: Assembly, now: datetime) -> None:
    """Each account's broker session, on a trading day."""
    if assembly.calendar.trading(trade_date_of(now)):
        assembly.desks.watch_sessions(now)


class Periodic:
    """The periodic loops over one `Assembly`."""

    def __init__(
        self,
        assembly: Assembly,
        clock: Clock,
        *,
        watch_lock: Callable[[datetime], None] | None = None,
    ) -> None:
        self._a = assembly
        self._clock = clock
        self._watch_lock = watch_lock
        self._exits = Every()
        self._housekeeping = Every()
        self._announcements = Every()
        self._calendar_alerted: date | None = None

    def loops(self, stop: asyncio.Event) -> list[Coroutine[object, object, None]]:
        """The loops to run as tasks until `stop` is set."""
        work = [
            self._loop(EXITS, self.exits, stop),
            self._loop(HOUSEKEEPING, self.housekeeping, stop),
        ]
        if self._a.parts.announcements is not None:
            work.append(self._loop(ANNOUNCEMENTS, self.announcements, stop))
        return work

    async def _loop(self, name: str, step: Callable[[datetime], None], stop: asyncio.Event) -> None:
        thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"hat-{name}")
        loop = asyncio.get_running_loop()
        try:
            while not stop.is_set():
                now = utc_now(self._clock)
                self._a.heartbeats.beat(name, now)
                try:
                    await loop.run_in_executor(thread, step, now)
                except Exception:
                    logger.exception("periodic loop %s failed", name)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), STEP_SECONDS)
        finally:
            thread.shutdown(wait=False)

    # --- the passes (blocking: each runs off the loop) -------------------------------------

    def exits(self, now: datetime) -> None:
        a = self._a
        if a.halted.is_set():
            return
        if not (a.calendar.trading(trade_date_of(now)) and a.calendar.is_open(now)):
            return
        steps: list[Step] = [
            (EXITS, timedelta(0), lambda: self.live_exits(now)),
            ("shadow", timedelta(0), lambda: a.shadow.run(now)),
            ("drawdown", DRAWDOWN_EVERY, lambda: a.intraday.drawdown(now, a.user_id)),
            ("entries", timedelta(0), lambda: a.desks.cancel_stale_entries(now, a.entries)),
        ]
        _run_steps(steps, self._exits, now)

    def housekeeping(self, now: datetime) -> None:
        a = self._a
        steps: list[Step] = [("calendar", CALENDAR_EVERY, lambda: self.check_calendar(now))]
        if self._watch_lock is not None:
            watch = self._watch_lock
            steps.append(("lock", MINUTE, lambda: watch(now)))
        if a.halted.is_set():
            _run_steps(steps, self._housekeeping, now)
            return
        steps.append(("feed_health", MINUTE, a.health_kill.retry))
        if a.calendar.trading(trade_date_of(now)):
            steps += [
                ("sessions", MINUTE, lambda: watch_sessions(a, now)),
                ("track", MINUTE, lambda: a.intraday.track_positions(now)),
                ("results", MINUTE, lambda: a.intraday.refresh_results(now)),
                ("sync", timedelta(0), lambda: a.desks.sync_due(now)),
                ("expire", MINUTE, lambda: a.desks.expire_all(now)),
                ("reconcile", RECONCILE_EVERY, lambda: a.desks.reconcile_all(now)),
                (
                    "resume",
                    timedelta(0),
                    lambda: a.desks.resume_entries(now, a.route.check, a.feed_stale),
                ),
            ]
        _run_steps(steps, self._housekeeping, now)

    def announcements(self, now: datetime) -> None:
        fetch = self._a.parts.announcements
        if fetch is None:
            return
        sf = self._a.data_session_factory  # the announcements are hat_live data
        steps: list[Step] = [
            (
                ANNOUNCEMENTS,
                poll_interval(now),
                lambda: poll_all(INTRADAY_FEEDS, fetch, sf, now=now),
            )
        ]
        _run_steps(steps, self._announcements, now)

    def check_calendar(self, now: datetime) -> None:
        """Read the calendar again (and hand it to the desks), and tell the owner once a day
        while it holds no trading day more than a week ahead (`calendar.short`): the trade-date
        jobs stop when it runs out."""
        a = self._a
        calendar = a.calendar.read()
        a.desks.use_calendar(calendar)
        today = trade_date_of(now)
        if calendar_short(calendar, today) and self._calendar_alerted != today:
            self._calendar_alerted = today
            last = calendar.trading_days()[-1:] if calendar is not None else []
            a.notifier.alert(
                ALERT_CALENDAR_SHORT, today, {"last": last[0].isoformat() if last else "none"}
            )

    def live_exits(self, now: datetime) -> int:
        """The live exit rules of each account's real positions through its `TradeManager`
        (stops follow, sales go out), each position alone: one whose view or rules raise leaves
        the others evaluated. The virtual positions are a step of their own after these."""
        a = self._a
        done = 0
        for account_id, manager in a.desks.managers():
            if not manager.orders_allowed(now):
                continue
            with a.session_factory() as session:
                for position in manager.open_positions(session, account_id):
                    try:
                        view = a.views(session, position)
                        if view is None:
                            continue
                        manager.on_price(session, position.id, view, now)
                        done += 1
                    except Exception:
                        session.rollback()
                        logger.exception("live exits of position %s failed", position.id)
        return done
