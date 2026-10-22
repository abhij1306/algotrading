"""The intraday jobs of the daily cycle (10-ops §3 steps 1-6, HAT-OPS-023, PR 17).

- `live.prepare` (08:45): load the day's daily view, subscribe the watchlist and the open
  positions, and hand the trigger monitor the watchlist candidates; a key that already has its
  signal (a restart mid-session) is marked fired so it never fires twice.
- `modes.apply` (09:15, the session open): the queued execution modes that are now in force.
- `gapscan` (09:16): the EP gappers whose setup passes become candidates of every EP bot and
  are subscribed; the scan alerts when it finished at or after 09:20.
- `exits.close_shadow` (15:20-15:25): the close-phase exits of the virtual positions no broker
  account's `TradeManager` handles.
- `watchlist.expire` (15:30, the session close): today's Watchlisted rows that never triggered end
  as Expired (HAT-EXEC-041).

The pre-open gap capture (09:00-09:08) is not a job: `LiveData` records each symbol's gap from the
feed's ticks. The trigger window opens at 09:20 with the first final 5-minute bar, and the regime
gate is evaluated then (`GateBoard`).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import Clock
from ..data.announcements import attached_announcements
from ..data.gapscan import ALERT_LATE, gap_scan_body, is_late
from ..data.live import LiveData
from ..data.mcp_client import McpClient
from ..data.subscriptions import LiveSubscriptions, load_inputs
from ..engine.live_entry import (
    EP_PLAYBOOK,
    EpInputs,
    TriggerService,
    ep_bot_candidates,
    ep_setup_passes,
    watchlist_candidates,
)
from ..engine.trades import expire_watchlist
from ..engine.trigger import TriggerMonitor
from ..engine.watchlist import bot_universe_members
from ..execution.manage import EXITS_CLOSE_FROM, EXITS_CLOSE_UNTIL
from ..execution.modes import apply_mode_changes
from ..models import HatBot, HatInstrument, HatPosition, HatSignal, HatWatchlistEntry
from ..notify import Notifier
from ..ops.jobs import JobError, JobResult, SessionFactory
from ..ops.scheduler import ScheduledJob
from ..telegram.reads import session_calendar
from ..time import utc_now
from .live import PriceBook, ShadowExits
from .market import DailyBook

__all__ = [
    "EXITS_CLOSE_SHADOW",
    "GAPSCAN",
    "LIVE_PREPARE",
    "MODES_APPLY",
    "WATCHLIST_EXPIRE",
    "Intraday",
    "intraday_jobs",
]

logger = logging.getLogger(__name__)

LIVE_PREPARE = "live.prepare"
MODES_APPLY = "modes.apply"
GAPSCAN = "gapscan"
EXITS_CLOSE_SHADOW = "exits.close_shadow"
WATCHLIST_EXPIRE = "watchlist.expire"
PREPARE_AT = time(8, 45)
MODES_AT = time(9, 15)
GAPSCAN_AT = time(9, 16)
GAPSCAN_UNTIL = time(9, 20)  # a scan that cannot start before the first bar is no use that day
WATCHLIST_EXPIRE_AT = time(15, 30)  # the session close: a Watchlisted row ends with its day
ONE_MINUTE = timedelta(minutes=1)


class Intraday:
    """The intraday job bodies over the live objects of one process."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        clock: Clock,
        book: DailyBook,
        prices: PriceBook,
        live: LiveData,
        service: TriggerService,
        subscriptions: LiveSubscriptions | None,
        shadow: ShadowExits,
        notifier: Notifier,
        gap_client: Callable[[], McpClient | None],
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._book = book
        self._prices = prices
        self._live = live
        self._service = service
        self._subs = subscriptions
        self._shadow = shadow
        self._notifier = notifier
        self._gap_client = gap_client

    def _now(self) -> datetime:
        return utc_now(self._clock)

    # --- 08:45 --------------------------------------------------------------------------------

    def prepare(self, day: date) -> JobResult:
        """Subscribe and arm the day. Safe to run again (a restart): the monitor is rebuilt and
        every key that already has a signal is marked fired."""
        self._book.reset()
        self._prices.reset()
        data = self._book.day(day)
        monitor = TriggerMonitor(self._live.builder)
        with self._session_factory() as session:
            calendar = session_calendar(session)
            candidates = watchlist_candidates(
                session,
                day,
                lambda iid: data.daily(session, iid),
                data.bars,
                calendar=calendar,
            )
            for candidate in candidates:
                monitor.add(candidate)
            for key in _signal_keys(session, day):
                monitor.mark_fired(key)
            inputs = load_inputs(session, day)
            self._prices.track_session(session, _day_instruments(session, day))
        self._service.monitor = monitor
        if self._subs is not None:
            self._subs.sync(inputs)
        return JobResult(rows=len(candidates))

    # --- 09:15 --------------------------------------------------------------------------------

    def apply_modes(self, _day: date) -> JobResult:
        with self._session_factory() as session:
            changed = apply_mode_changes(session, self._now())
            session.commit()
        return JobResult(rows=changed)

    # --- 09:16 --------------------------------------------------------------------------------

    def gapscan(self, day: date) -> JobResult:
        client = self._gap_client()
        if client is None:
            raise JobError("mcp_not_configured")
        result = gap_scan_body(
            client=client,
            session_factory=self._session_factory,
            trade_date=day,
            universe=self._in_a_universe(day),
            on_candidates=lambda found: self.on_gappers(day, found),
        )()
        if is_late(self._now(), day):
            self._notifier.alert(ALERT_LATE, day, {})
        return result

    def _in_a_universe(self, day: date) -> Callable[[HatInstrument], bool]:
        with self._session_factory() as session:
            members: set[uuid.UUID] = set()
            for bot in session.scalars(select(HatBot).where(HatBot.status == "active")):
                members |= bot_universe_members(session, bot, day)
        return lambda instrument: instrument.id in members

    def on_gappers(self, day: date, found: Mapping[str, uuid.UUID]) -> int:
        """The EP candidates of each EP bot among the gappers: setup passed on the bars before
        `day`, open price from the feed's pre-open record, announcements of the last 24 hours."""
        data = self._book.day(day)
        gaps = self._live.gaps.records()
        now = self._now()
        with self._session_factory() as session:
            items = [
                EpInputs(
                    instrument_id=iid,
                    symbol=symbol,
                    open_price=gaps[symbol].open if symbol in gaps else None,
                    announcements=tuple(
                        a.published_at for a in attached_announcements(session, iid, now)
                    ),
                )
                for symbol, iid in found.items()
            ]
            candidates = [
                c
                for c in ep_bot_candidates(
                    session,
                    day,
                    items,
                    lambda iid: data.daily(session, iid),
                    data.bars,
                    calendar=session_calendar(session),
                )
                if c.playbook.name == EP_PLAYBOOK
                and ep_setup_passes(c.playbook, data.bars(c.instrument_id), day)
            ]
            self._prices.track_session(session, {c.instrument_id for c in candidates})
        for candidate in candidates:
            self._service.monitor.add(candidate)
        if self._subs is not None and candidates:
            self._subs.add_candidates({c.symbol for c in candidates})
        return len(candidates)

    # --- 15:20-15:25 --------------------------------------------------------------------------

    def close_shadow(self, _day: date) -> JobResult:
        now = self._now()
        return JobResult(rows=self._shadow.close(now))

    # --- 15:30 --------------------------------------------------------------------------------

    def expire_watchlist(self, day: date) -> JobResult:
        with self._session_factory() as session:
            expired = expire_watchlist(session, day)
            session.commit()
        return JobResult(rows=expired)

    def live_exits(self) -> int:
        """One pass of the live exit rules over the open virtual positions."""
        return self._shadow.run(self._now())


def _signal_keys(session: Session, day: date) -> list[tuple[uuid.UUID, str, uuid.UUID, date]]:
    return [
        (s.bot_id, s.playbook, s.instrument_id, s.trade_date)
        for s in session.scalars(select(HatSignal).where(HatSignal.trade_date == day))
    ]


def _day_instruments(session: Session, day: date) -> set[uuid.UUID]:
    watched = session.scalars(
        select(HatWatchlistEntry.instrument_id).where(HatWatchlistEntry.trade_date == day)
    )
    held = session.scalars(select(HatPosition.instrument_id).where(HatPosition.closed_at.is_(None)))
    return {*watched, *held}


def intraday_jobs(intraday: Intraday) -> tuple[ScheduledJob, ...]:
    return (
        ScheduledJob(LIVE_PREPARE, PREPARE_AT, intraday.prepare, retry_after=ONE_MINUTE),
        ScheduledJob(MODES_APPLY, MODES_AT, intraday.apply_modes, retry_after=ONE_MINUTE),
        ScheduledJob(
            GAPSCAN,
            GAPSCAN_AT,
            intraday.gapscan,
            until=GAPSCAN_UNTIL,
            retry_after=ONE_MINUTE,
            after=(LIVE_PREPARE,),
        ),
        ScheduledJob(
            EXITS_CLOSE_SHADOW,
            EXITS_CLOSE_FROM,
            intraday.close_shadow,
            until=EXITS_CLOSE_UNTIL,
            retry_after=ONE_MINUTE,
        ),
        ScheduledJob(
            WATCHLIST_EXPIRE,
            WATCHLIST_EXPIRE_AT,
            intraday.expire_watchlist,
            retry_after=ONE_MINUTE,
        ),
    )
