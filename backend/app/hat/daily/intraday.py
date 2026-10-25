"""The intraday jobs of the daily cycle (HAT-OPS-023, PR 17).

- `live.prepare` (08:45): load the day's daily view, subscribe the watchlist and the open
  positions, and hand the trigger monitor the watchlist candidates; a key that already has its
  signal (a restart mid-session) is marked fired so it never fires twice, and a restart after the
  gap scan rebuilds the EP candidates it recorded.
- `modes.apply` (09:15, the session open): the queued execution modes that are now in force.
- `gapscan` (09:16): the gappers whose setup passes become candidates of each active bot's
  pre-open playbooks (`Playbook.preopen`, such as the episodic pivot) and are subscribed; the
  gappers and their opens are recorded (`gapscan.completed`) for a restart; the scan alerts when
  it finished at or after 09:20.
- `exits.close_shadow` (15:20-15:25): the close-phase exits of the virtual positions no broker
  account's `TradeManager` handles.
- `watchlist.expire` (15:30, the session close): today's Watchlisted rows that never triggered end
  as Expired (HAT-EXEC-041).

The pre-open gap capture (09:00-09:08) is not a job: `LiveData` records each symbol's gap from the
feed's ticks. The trigger window opens at 09:20 with the first final 5-minute bar, and the regime
gate is evaluated then (`GateBoard`).

The times are the regular session's. The open-phase jobs (`live.prepare`, `modes.apply`, the gap
scan) move with a session that opens at another time (`open_shift`: a 14:20 Muhurat open runs
them at 13:50, 14:20 and 14:21-14:25), the close-phase ones (`exits.close_shadow`,
`watchlist.expire`) with one that closes at another time (`close_shift`).

`track_positions` (housekeeping, every minute in the session) prices and subscribes an open
position that `live.prepare` did not know: a late fill on a name not on the day's watchlist is
marked and exited on its own price, not yesterday's close.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import Clock
from ..data.announcements import attached_announcements
from ..data.gapscan import ALERT_LATE, gap_scan_body, is_late
from ..data.live import LiveData, usable_price
from ..data.mcp_client import McpClient
from ..data.subscriptions import LiveSubscriptions, load_inputs
from ..engine.live_entry import (
    EpInputs,
    TriggerService,
    ep_bot_candidates,
    ep_setup_passes,
    watchlist_candidates,
)
from ..engine.portfolio.allocation_service import policy_at
from ..engine.shadow import expire_watchlist
from ..engine.trigger import TriggerMonitor
from ..engine.watchlist import bot_universe_members
from ..events import record
from ..execution.manage import EXITS_CLOSE_FROM, EXITS_CLOSE_UNTIL
from ..execution.mode_changes import apply_mode_changes
from ..models import (
    HatBot,
    HatEvent,
    HatInstrument,
    HatJobRun,
    HatPosition,
    HatSignal,
    HatWatchlistEntry,
)
from ..money import require_exact
from ..notify import Notifier
from ..ops.jobs import JobError, JobResult, SessionFactory
from ..ops.scheduler import ScheduledJob
from ..risk.drawdown import ALERT_KILL_DRAWDOWN, evaluate_drawdown_kills, live_equity
from ..risk.kill import MARK_CLOSE, MARK_STOP
from ..time import trade_date_of, utc_now
from .exits import ShadowExits
from .gates import CalendarSource
from .market import DailyBook
from .prices import PriceBook

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
GAPPERS_RECORDED = "gapscan.completed"

Gappers = tuple[Mapping[str, uuid.UUID], Mapping[str, Decimal]]  # symbol -> id, symbol -> open


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
        calendar: CalendarSource,
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
        self._calendar = calendar

    def _now(self) -> datetime:
        return utc_now(self._clock)

    # --- during the session -------------------------------------------------------------------

    def drawdown(self, now: datetime, user_id: uuid.UUID) -> list[str]:
        """The drawdown kills on equity marked to market now (P5, owner decision X9): each
        bot's open positions at their latest trade; before an instrument's first trade of the
        session, at its previous official close (D210); once its trade goes stale, at the lower
        of that trade and the stop (`kill.mark_of`). Each scope tripped is alerted, with how many
        holdings were marked on each basis but a fresh trade. Returns the scopes tripped."""
        day = trade_date_of(now)
        data = self._book.day(day)
        with self._session_factory() as session:

            def mark(instrument_id: uuid.UUID) -> tuple[Decimal | None, bool, Decimal | None]:
                quote = self._prices.quote(instrument_id)
                if quote is not None:
                    return quote.price, usable_price(quote, now)[1] is None, None
                try:
                    close = data.daily(session, instrument_id).prev_close
                except Exception:  # unreadable: the stop basis, never a higher mark
                    logger.exception("previous close of %s unreadable", instrument_id)
                    close = None
                return None, False, close

            live, fallbacks = live_equity(session, now, mark)
            tripped = evaluate_drawdown_kills(session, policy_at(session, user_id, day), now, live)
            session.commit()
        if fallbacks[MARK_STOP] or fallbacks[MARK_CLOSE]:
            logger.warning(
                "intraday drawdown: %d holdings marked at the previous close, %d at the stop basis",
                fallbacks[MARK_CLOSE],
                fallbacks[MARK_STOP],
            )
        for scope in tripped:
            self._notifier.alert(
                ALERT_KILL_DRAWDOWN,
                day,
                {
                    "scope": scope,
                    "basis": "intraday",
                    "fallback_marks": fallbacks[MARK_STOP],
                    "close_marks": fallbacks[MARK_CLOSE],
                },
            )
        return tripped

    # --- 08:45 --------------------------------------------------------------------------------

    def prepare(self, day: date) -> JobResult:
        """Subscribe and arm the day. Safe to run again (a restart): the monitor is rebuilt and
        every key that already has a signal is marked fired."""
        self._book.reset()
        self._prices.reset()
        data = self._book.day(day)
        monitor = TriggerMonitor(self._live.builder)
        with self._session_factory() as session:
            candidates = watchlist_candidates(
                session,
                day,
                lambda iid, stop: data.daily(session, iid, stop),
                data.bars,
                calendar=self._calendar(),
            )
            for candidate in candidates:
                monitor.add(candidate)
            for key in _signal_keys(session, day):
                monitor.mark_fired(key)
            inputs = load_inputs(session, day)
            self._prices.track_session(session, _day_instruments(session, day))
            gappers = _recorded_gappers(session, day)
        self._service.monitor = monitor
        if self._subs is not None:
            self._subs.sync(inputs)
        if gappers is not None:  # a restart after the gap scan: its EP candidates again
            self._add_gappers(day, *gappers)
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
            on_candidates=lambda found, opens: self.on_gappers(day, found, opens),
        )()
        if is_late(self._now(), day, self.open_shift(day)):
            self._notifier.alert(ALERT_LATE, day, {})
        return result

    def _in_a_universe(self, day: date) -> Callable[[HatInstrument], bool]:
        with self._session_factory() as session:
            members: set[uuid.UUID] = set()
            for bot in session.scalars(select(HatBot).where(HatBot.status == "active")):
                members |= bot_universe_members(session, bot, day)
        return lambda instrument: instrument.id in members

    def on_gappers(
        self,
        day: date,
        found: Mapping[str, uuid.UUID],
        scanned: Mapping[str, Decimal] | None = None,
    ) -> int:
        """Record the gappers with their opens (a restart rebuilds the candidates from them: the
        record is gone by then), then add the EP candidates. The open is the feed's pre-open
        record of that trade date, else the open the scan's listing gave (a gapper is usually
        not subscribed before the scan, so the feed has no record of it)."""
        gaps = self._live.gaps.records(day)
        listed = scanned or {}
        opens = {
            symbol: gaps[symbol].open if symbol in gaps else listed[symbol]
            for symbol in found
            if symbol in gaps or symbol in listed
        }
        with self._session_factory() as session:
            _record_gappers(session, day, found, opens, self._now())
            session.commit()
        return self._add_gappers(day, found, opens)

    def _add_gappers(
        self, day: date, found: Mapping[str, uuid.UUID], opens: Mapping[str, Decimal]
    ) -> int:
        """The candidates of each bot's pre-open playbooks among the gappers: setup passed on the
        bars before `day`, the day's open, announcements of the last 24 hours."""
        data = self._book.day(day)
        now = self._now()
        with self._session_factory() as session:
            items = [
                EpInputs(
                    instrument_id=iid,
                    symbol=symbol,
                    open_price=opens.get(symbol),
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
                    lambda iid, stop: data.daily(session, iid, stop),
                    data.bars,
                    calendar=self._calendar(),
                )
                if ep_setup_passes(c.playbook, data.bars(c.instrument_id), day)
            ]
            self._prices.track_session(session, {c.instrument_id for c in candidates})
        for candidate in candidates:
            self._service.monitor.add(candidate)
        if self._subs is not None and candidates:
            self._subs.add_candidates({c.symbol for c in candidates})
        return len(candidates)

    # --- during the session (housekeeping) ------------------------------------------------------

    def track_positions(self, now: datetime) -> int:
        """Price and subscribe each open position's instrument that is not yet priced by id or
        not in the subscriptions' inputs (opened after `live.prepare`, audit A3 F5); each is
        checked on its own, so an instrument priced earlier but no longer subscribed rejoins the
        feed. Returns how many instruments needed either; nothing outside an open session."""
        calendar = self._calendar()
        if calendar is None or not calendar.is_open(now):
            return 0
        with self._session_factory() as session:
            held = _open_position_symbols(session)
            unpriced = {iid for iid in held if not self._prices.tracks(iid)}
            if unpriced:
                self._prices.track_session(session, unpriced)
        wanted = self._subs.desired() if self._subs is not None else frozenset(held.values())
        unsubscribed = {iid for iid, symbol in held.items() if symbol not in wanted}
        if self._subs is not None and unsubscribed:
            self._subs.add_positions({held[iid] for iid in unsubscribed})
        added = unpriced | unsubscribed
        if added:
            logger.info("tracking %d positions opened after the day was armed", len(added))
        return len(added)

    # --- the session's shifts -----------------------------------------------------------------

    def open_shift(self, day: date) -> timedelta:
        """How far `day`'s session opens from the regular open (the open jobs move with it)."""
        calendar = self._calendar()
        return calendar.open_shift(day) if calendar is not None else timedelta(0)

    # --- 15:20-15:25 --------------------------------------------------------------------------

    def close_shift(self, day: date) -> timedelta:
        """How far `day`'s session closes from the regular close (the close jobs move with it)."""
        calendar = self._calendar()
        return calendar.close_shift(day) if calendar is not None else timedelta(0)

    def close_shadow(self, _day: date) -> JobResult:
        now = self._now()
        return JobResult(rows=self._shadow.close(now, self.close_shift(trade_date_of(now))))

    # --- 15:30 --------------------------------------------------------------------------------

    def expire_watchlist(self, day: date) -> JobResult:
        with self._session_factory() as session:
            expired = expire_watchlist(session, day)
            session.commit()
        return JobResult(rows=expired)


def _gapscan_run(session: Session, day: date) -> uuid.UUID | None:
    return session.scalar(
        select(HatJobRun.id).where(HatJobRun.job == GAPSCAN, HatJobRun.trade_date == day)
    )


def _record_gappers(
    session: Session,
    day: date,
    found: Mapping[str, uuid.UUID],
    opens: Mapping[str, Decimal],
    now: datetime,
) -> None:
    run_id = _gapscan_run(session, day)
    if run_id is None:  # not run by the scheduler: nothing to rebuild from
        return
    record(
        session,
        aggregate_type="ingest",
        aggregate_id=run_id,
        event_type=GAPPERS_RECORDED,
        actor="scheduler",
        payload={
            "trade_date": day.isoformat(),
            "gappers": [
                {
                    "symbol": symbol,
                    "instrument_id": str(iid),
                    "open": str(opens[symbol]) if symbol in opens else None,
                }
                for symbol, iid in sorted(found.items())
            ],
        },
        occurred_at=now,
    )


def _recorded_gappers(session: Session, day: date) -> Gappers | None:
    """The gappers the day's last gap scan recorded; None before it ran."""
    run_id = _gapscan_run(session, day)
    if run_id is None:
        return None
    payload = session.scalar(
        select(HatEvent.payload)
        .where(HatEvent.aggregate_id == run_id, HatEvent.event_type == GAPPERS_RECORDED)
        .order_by(HatEvent.id.desc())
        .limit(1)
    )
    if payload is None:
        return None
    rows = payload["gappers"]
    found = {row["symbol"]: uuid.UUID(row["instrument_id"]) for row in rows}
    opens = {
        row["symbol"]: require_exact(row["open"], "open") for row in rows if row["open"] is not None
    }
    return found, opens


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


def _open_position_symbols(session: Session) -> dict[uuid.UUID, str]:
    """Each open position's instrument with its symbol (virtual ones too, as `load_inputs`)."""
    rows = session.execute(
        select(HatInstrument.id, HatInstrument.symbol)
        .join(HatPosition, HatPosition.instrument_id == HatInstrument.id)
        .where(HatPosition.closed_at.is_(None))
    )
    return dict(rows.tuples().all())


def intraday_jobs(intraday: Intraday) -> tuple[ScheduledJob, ...]:
    return (
        ScheduledJob(
            LIVE_PREPARE,
            PREPARE_AT,
            intraday.prepare,
            retry_after=ONE_MINUTE,
            shift=intraday.open_shift,
        ),
        ScheduledJob(
            MODES_APPLY,
            MODES_AT,
            intraday.apply_modes,
            retry_after=ONE_MINUTE,
            shift=intraday.open_shift,
        ),
        ScheduledJob(
            GAPSCAN,
            GAPSCAN_AT,
            intraday.gapscan,
            until=GAPSCAN_UNTIL,
            retry_after=ONE_MINUTE,
            after=(LIVE_PREPARE,),
            shift=intraday.open_shift,
        ),
        ScheduledJob(
            EXITS_CLOSE_SHADOW,
            EXITS_CLOSE_FROM,
            intraday.close_shadow,
            until=EXITS_CLOSE_UNTIL,
            retry_after=ONE_MINUTE,
            shift=intraday.close_shift,
        ),
        ScheduledJob(
            WATCHLIST_EXPIRE,
            WATCHLIST_EXPIRE_AT,
            intraday.expire_watchlist,
            retry_after=ONE_MINUTE,
            shift=intraday.close_shift,
        ),
    )
