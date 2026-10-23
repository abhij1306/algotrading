"""Protective stops at the broker for real positions (HAT-EXEC-060..064, HAT-DEC-B17,
HAT-OPS-024; the model is `verification/tla/StopPlacement.tla`).

`StopDesk` keeps one resting broker stop under every open real position:

- **On a fill** (`protect`): with no working stop, an intraday stop order (purpose `stop_day`,
  SL-M SELL CNC for the open quantity at the position's stop) while the market is open and
  before the 15:25 swap, a GTT stop (`stop_gtt`) otherwise (a day stop placed after the swap
  would expire at the close with no GTT behind it); a working stop for another quantity is
  resized. A rejected stop is retried once with the next tag, then alerted (`stop.not_placed`);
  the next sync tries again. A closed position's working stops are cancelled.
- **The entry remainder** (`cancel_entry`, F2): once the position holds no shares, or before a
  sale of all of them (`make_room` to 0), the unfilled rest of a part-filled entry is cancelled
  at the broker, so it cannot fill under a position HAT is closing. The position closes only once
  the broker shows the entry ended; a fill that lands first adds to it and is protected. Each
  entry's cancel is sent once per desk, again only after the broker could not be reached.
- **An entry nothing of which filled** (`cancel_unfilled_entry`, PR 49): cancelled at the
  broker the same way when its break failed or it waited too long (`entry_timeout.EntryWatch`
  decides). One that filled in part keeps its remainder working (the partial-entry policy).
- **When the stop rises** (`follow`): the working stop is modified up to the new price (day:
  `modify_order`; GTT: `modify_gtt`, event `gtt.refreshed`), never down (nor by the verify).
  The broker trigger is the stop rounded down to the instrument's tick, so it is never above
  `current_stop`.
- **At ~15:25** (`swap`): the GTT is placed first and the intraday stop is cancelled only once
  the GTT is confirmed placed; a GTT that fails leaves the intraday stop working and raises
  `gtt.swap_failed`. A position with a working exit order is not swapped (its exit comes first).
- **Nightly** (`verify`): every open real position's GTT is refreshed to the current stop
  (`gtt.refreshed`). A GTT the broker no longer knows (triggered, cancelled or never confirmed)
  is alerted (`gtt.missing`) and placed again; when the broker cannot be reached the position
  is alerted (`gtt.unverified`) and nothing is placed, since the old GTT may still rest there.
  A GTT placed `GTT_RENEW_AFTER` (300 days) ago or more is cancelled and placed again (the
  broker expires a GTT after a year, and a modify may not reset that clock; PR 47).
- **Before the open, around an ex-date** (`check_gtt`, PR 47, HAT-EXEC-145): Fyers cancels a
  resting GTT before a corporate action's ex-date (a dividend's too) and on a series change. A
  live GTT the broker's GTT book no longer holds is ended `cancelled`, alerted (`gtt.missing`)
  and the position protected again: a GTT before the open, else (refused, or once the session is
  open) the fill sync's intraday stop from 09:15.
- **A series change** (`_target`, HAT-EXEC-146): a position whose instrument moved to another
  series (EQ to BE) has its stops placed under the successor's symbol (`series.successor`).
- **A lost answer** (`settle_lost`, HAT-EXEC-065): a stop whose placement answer was lost is
  settled against the broker once `settle_after` has passed since it was sent (a request still
  in flight could land later): a GTT against the broker's GTT book by its tag (else, for a GTT
  shown untagged, by symbol, quantity and trigger: `submit.find_gtt`), a day stop by its tag.
  Found, it is adopted as placed; absent after a good read, it never landed: it ends `expired`
  and the position is protected again.
  While a stop's outcome is unknown it never holds a sale back (`make_room` skips it: a resting
  CNC stop makes the broker refuse the sale, the safe way round).

Every order goes through `submit.reserve_order` and `submit.send_order` (trading-safety rule 1);
modifies and cancels go straight to the `HatBroker` contract with the broker id `send_order`
stored. Nothing here checks a kill switch: stops are never blocked. Alerts carry fixed
codes only, never broker text.
"""

from __future__ import annotations

import functools
import logging
import threading
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..engine.rescale import applied_factors, pending_actions
from ..errors import HatError
from ..events import record
from ..models import HatBot, HatEvent, HatInstrument, HatOrder, HatPosition, HatSignal
from ..notify import LogNotifier, Notifier
from ..risk.sessions import SessionCalendar, SessionState
from ..time import IST, as_utc, day_bounds, trade_date_of, utc_now
from .broker import CODE_UNAVAILABLE as BROKER_UNAVAILABLE
from .broker import (
    CODES_NOT_SENT,
    AckResult,
    BrokerUnavailableError,
    GttModify,
    HatBroker,
    OrderType,
    Side,
)
from .gtt import gtt_limit_price
from .guard import ExecutionGuard
from .series import broker_symbol, series_changed
from .submit import (
    EXIT_SALES,
    OrderRefusedError,
    OrderRequest,
    SendOutcome,
    UnresolvedOrderError,
    adopt_broker_order,
    adopt_gtt,
    end_lost_order,
    find_gtt,
    reserve_order,
    send_order,
)

__all__ = [
    "ALERT_EXIT_NOT_PLACED",
    "ALERT_GTT_MISSING",
    "ALERT_GTT_SWAP_FAILED",
    "ALERT_GTT_UNFILLED",
    "ALERT_GTT_UNVERIFIED",
    "ALERT_STOP_NOT_MOVED",
    "ALERT_STOP_NOT_PLACED",
    "LIVE",
    "LOST_ANSWER_SETTLE",
    "STOP_DAY",
    "STOP_GTT",
    "SWAP_AT",
    "WORKING",
    "StopDesk",
    "TickSource",
    "UNSURE",
    "broker_trigger",
]

logger = logging.getLogger(__name__)

ENTRY = "entry"

STOP_DAY = "stop_day"
STOP_GTT = "stop_gtt"
EVENT_GTT_REFRESHED = "gtt.refreshed"
EVENT_GTT_TRIGGERED = "gtt.triggered"
WORKING = ("pending", "unconfirmed", "placed", "partially_filled")
LIVE = ("placed", "partially_filled")
UNSURE = ("pending", "unconfirmed")
MAX_STOP_REJECTIONS = 2  # the original and one retry (HAT-OPS-032)
SWAP_AT = time(15, 25)  # IST: from here on a new stop is a GTT (HAT-EXEC-061)
# How long after its send an order whose answer was lost is settled against the broker's books:
# the guarded call gives up after 1.5 s, but the SDK request may still be in flight and land.
LOST_ANSWER_SETTLE = timedelta(seconds=60)
# A GTT this old is placed again by the nightly verify: Fyers expires a GTT a year after it was
# placed, and a modify may not reset that clock (research G24; [verify on a live account]).
CODE_SERIES_UNMAPPED = "series_unmapped"  # the new series has no Fyers symbol
GTT_RENEW_AFTER = timedelta(days=300)

ALERT_STOP_NOT_PLACED = "stop.not_placed"
ALERT_EXIT_NOT_PLACED = "exit.not_placed"
ALERT_STOP_NOT_MOVED = "stop.not_moved"
ALERT_GTT_SWAP_FAILED = "gtt.swap_failed"
ALERT_GTT_MISSING = "gtt.missing"
ALERT_GTT_UNVERIFIED = "gtt.unverified"
ALERT_GTT_UNFILLED = "gtt.unfilled"

# Outcomes (fixed words) the jobs count.
PROTECTED = "protected"
PLACED = "placed"
FAILED = "failed"
CLOSED = "closed"
SWAPPED = "swapped"
EXIT_WORKING = "exit_working"
VERIFIED = "verified"
REPLACED = "replaced"
UNVERIFIED = "unverified"

# The instrument's tick for a position's stop prices (`data.ticks.tick_for_date`, HAT-EXEC-053).
TickSource = Callable[[Session, HatPosition], Decimal | None]


def broker_trigger(stop: Decimal, tick: Decimal | None) -> Decimal:
    """The broker trigger of a stop: rounded down to the tick (never above the stop); the stop
    itself when the tick is unknown (stops from the engine are already on the tick)."""
    if tick is None or not tick.is_finite() or tick <= 0:
        return stop
    return (stop / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


@dataclass(frozen=True)
class _Target:
    """What a position's stop order is placed for."""

    position: HatPosition
    signal: HatSignal
    account_id: uuid.UUID
    symbol: str
    # The series changed and the new series has no stored Fyers symbol (a BZ stock): the
    # adapter would fall back to the old `-EQ` symbol, so no stop is sent (HAT-EXEC-146).
    unmapped: bool = False


def _serialised(method: Callable[..., str]) -> Callable[..., str]:
    """Hold the position's stop lock for the call. The fill sync and a trade job run on their
    own threads over one desk: without it, both could see a cancelled stop and each place a new
    one, leaving two GTTs over the same shares."""

    @functools.wraps(method)
    def locked(self: StopDesk, session: Session, position_id: uuid.UUID, *args: object) -> str:
        with self._stop_lock(position_id):
            return method(self, session, position_id, *args)

    return locked


class StopDesk:
    """Broker stops for one broker account's session; `guard` is the account's order gate (it
    never holds a stop back: the reconciliation gates entries only, D118)."""

    def __init__(
        self,
        broker: HatBroker,
        guard: ExecutionGuard,
        *,
        tick_for: TickSource,
        calendar: SessionCalendar | None = None,
        notifier: Notifier | None = None,
        settle_after: timedelta = LOST_ANSWER_SETTLE,
        wall: Callable[[], datetime] = utc_now,
    ) -> None:
        self._broker = broker
        self._guard = guard
        self._tick_for = tick_for
        self._calendar = calendar
        self._notifier = notifier or LogNotifier()
        # A lost answer is settled `settle_after` after its send, by the wall clock the send was
        # stamped with (`placed_at`), not the caller's `now`.
        self._settle_after = settle_after
        self._wall = wall
        # Entries whose remainder the broker was asked to cancel (`cancel_entry`): not asked again.
        self._entry_cancels: set[uuid.UUID] = set()
        # One re-entrant lock per position around what places a stop (`_serialised`).
        self._stop_locks: dict[uuid.UUID, threading.RLock] = {}
        self._stop_locks_guard = threading.Lock()

    def _stop_lock(self, position_id: uuid.UUID) -> threading.RLock:
        with self._stop_locks_guard:
            return self._stop_locks.setdefault(position_id, threading.RLock())

    def use_calendar(self, calendar: SessionCalendar | None) -> None:
        """The trading calendar read again (a refresh, a new year's rows)."""
        self._calendar = calendar

    # --- reading ------------------------------------------------------------------------------

    @staticmethod
    def stops_of(session: Session, position_id: uuid.UUID, *statuses: str) -> list[HatOrder]:
        """The position's real stop orders in `statuses` (default: working), oldest first."""
        return list(
            session.scalars(
                select(HatOrder)
                .where(
                    HatOrder.position_id == position_id,
                    HatOrder.is_virtual.is_(False),
                    HatOrder.purpose.in_((STOP_DAY, STOP_GTT)),
                    HatOrder.status.in_(statuses or WORKING),
                )
                .order_by(HatOrder.tag)
            )
        )

    @staticmethod
    def exit_working(
        session: Session, position_id: uuid.UUID, statuses: tuple[str, ...] = WORKING
    ) -> bool:
        """A sale of the position may still be at the broker (`statuses`: only those)."""
        found = session.scalar(
            select(HatOrder.id).where(
                HatOrder.position_id == position_id,
                HatOrder.is_virtual.is_(False),
                HatOrder.purpose.in_(EXIT_SALES),
                HatOrder.status.in_(statuses),
            )
        )
        return found is not None

    @staticmethod
    def triggered_gtts(session: Session, position_id: uuid.UUID, now: datetime) -> set[uuid.UUID]:
        """The position's live GTTs that triggered today with their sale still working at the
        broker (`gtt.triggered`, HAT-EXEC-066): such a sale holds the shares and the fill sync has
        it sold at the market, so no other sale is sent and the GTT is not moved meanwhile. The
        sale is a DAY order, so a trigger of an earlier trade date holds nothing back: one the
        sync never saw end leaves the GTT to the exits and the nightly verify as before."""
        start, _ = day_bounds(trade_date_of(now))
        return set(
            session.scalars(
                select(HatOrder.id)
                .join(HatEvent, HatEvent.aggregate_id == HatOrder.id)
                .where(
                    HatOrder.position_id == position_id,
                    HatOrder.is_virtual.is_(False),
                    HatOrder.purpose == STOP_GTT,
                    HatOrder.status.in_(LIVE),
                    HatEvent.aggregate_type == "order",
                    HatEvent.event_type == EVENT_GTT_TRIGGERED,
                    HatEvent.occurred_at >= start,
                )
            )
        )

    def _day_stop_time(self, now: datetime) -> bool:
        """In the session and before the 15:25 swap: a new stop is an intraday order."""
        return (
            self._calendar is not None
            and self._calendar.state(now) is SessionState.OPEN
            and now.astimezone(IST).time() < SWAP_AT
        )

    def _target(self, session: Session, position_id: uuid.UUID) -> _Target:
        position = session.get(HatPosition, position_id)
        if position is None or position.is_virtual:
            raise HatError("not a real position")
        session.refresh(position)
        signal = session.get(HatSignal, position.signal_id)
        bot = session.get(HatBot, position.bot_id)
        moved = series_changed(session, position)
        # A series change re-maps the symbol stops go out under (HAT-EXEC-146): the old series'
        # Fyers symbol takes no order.
        symbol = (
            moved.symbol
            if moved is not None
            else session.scalar(
                select(HatInstrument.symbol).where(HatInstrument.id == position.instrument_id)
            )
        )
        if signal is None or bot is None or symbol is None:
            raise HatError("position without signal, bot or instrument")
        unmapped = moved is not None and broker_symbol(session, moved.id) is None
        return _Target(position, signal, bot.broker_account_id, symbol, unmapped)

    # --- events and alerts --------------------------------------------------------------------

    @staticmethod
    def _order_event(
        session: Session, row: HatOrder, target: _Target, event_type: str, now: datetime
    ) -> None:
        record(
            session,
            aggregate_type="order",
            aggregate_id=row.id,
            event_type=event_type,
            actor="engine",
            payload={
                "tag": row.tag,
                "purpose": row.purpose,
                "broker_order_id": row.broker_order_id,
                "qty": row.qty,
                "trigger_price": None if row.trigger_price is None else str(row.trigger_price),
            },
            correlation_id=target.signal.id,
            bot_id=target.signal.bot_id,
            occurred_at=now,
        )

    def _alert(self, code: str, target: _Target, now: datetime, reason: str) -> None:
        self._notifier.alert(
            code,
            trade_date_of(now),
            {
                "bot_id": str(target.signal.bot_id),
                "signal_id": str(target.signal.id),
                "position_id": str(target.position.id),
                "code": reason,
            },
        )

    # --- placing ------------------------------------------------------------------------------

    def _request(self, target: _Target, purpose: str, tick: Decimal | None) -> OrderRequest:
        position = target.position
        trigger = broker_trigger(Decimal(position.current_stop), tick)
        gtt = purpose == STOP_GTT
        return OrderRequest(
            bot_id=target.signal.bot_id,
            signal_id=target.signal.id,
            broker_account_id=target.account_id,
            purpose=purpose,
            side=Side.SELL,
            qty=position.qty_open,
            order_type=OrderType.SL if gtt else OrderType.SL_M,
            symbol=target.symbol,
            position_id=position.id,
            price=gtt_limit_price(trigger, tick) if gtt and tick is not None else None,
            trigger_price=trigger,
        )

    def _send(
        self, session: Session, target: _Target, tag: str, tick: Decimal | None
    ) -> SendOutcome | str:
        """One send of a reserved stop: the outcome, or a refusal code (nothing was sent, or it
        cannot be told)."""
        if target.unmapped:
            self._release_unsent(session, tag)
            return CODE_SERIES_UNMAPPED
        try:
            return send_order(
                session,
                self._broker,
                self._guard,
                tag,
                symbol=target.symbol,
                bot_id=target.signal.bot_id,
                tick_size=tick,
            )
        except OrderRefusedError as exc:
            self._release_unsent(session, tag)
            return exc.code
        except BrokerUnavailableError:
            self._release_unsent(session, tag)
            return BROKER_UNAVAILABLE
        except UnresolvedOrderError:
            return "unresolved"

    @staticmethod
    def _release_unsent(session: Session, tag: str) -> None:
        """Close a reserved stop that provably never reached the broker as cancelled."""
        session.rollback()
        row = session.scalar(select(HatOrder).where(HatOrder.tag == tag))
        if row is not None and row.status == "pending" and row.placed_at is None:
            row.status = "cancelled"
            session.commit()

    def _place(
        self,
        session: Session,
        target: _Target,
        purpose: str,
        now: datetime,
        alert: str = ALERT_STOP_NOT_PLACED,
    ) -> bool:
        """Place a new stop of `purpose` for the open quantity; a rejection is retried once with
        the next tag. True once the broker confirmed it; an unconfirmed send is left to the next
        sync (a day stop is looked up by its tag; a GTT waits for the nightly verify). A failure
        raises `alert`."""
        if target.unmapped:
            self._alert(alert, target, now, CODE_SERIES_UNMAPPED)  # no tag spent
            return False
        tick = self._tick_for(session, target.position)
        reason = "broker_rejected"
        for _ in range(MAX_STOP_REJECTIONS):
            try:
                tag = reserve_order(session, self._request(target, purpose, tick))
            except HatError as exc:  # no limit price, the 37th tag, a failed reservation
                session.rollback()
                self._alert(alert, target, now, getattr(exc, "code", "reservation_failed"))
                return False
            sent = self._send(session, target, tag, tick)
            if isinstance(sent, str):
                self._alert(alert, target, now, sent)
                return False
            if sent.status in LIVE:
                return True
            if sent.status != "rejected":
                self._alert(alert, target, now, sent.code)  # unconfirmed: told, left to sync
                return False
            reason = sent.code
        self._alert(alert, target, now, reason)
        return False

    def _resend(self, session: Session, target: _Target, row: HatOrder) -> bool:
        """Send a stop reserved earlier whose outcome is not known yet (by its own tag)."""
        sent = self._send(session, target, row.tag, self._tick_for(session, target.position))
        return not isinstance(sent, str) and sent.status in LIVE

    # --- lost answers (HAT-EXEC-065) ---------------------------------------------------------

    @staticmethod
    def lost(row: HatOrder) -> bool:
        """The stop was sent and its answer lost (or the process stopped mid-send)."""
        return row.broker_order_id is None and (
            row.status == "unconfirmed" or (row.status == "pending" and row.placed_at is not None)
        )

    def _settled_long_enough(self, row: HatOrder) -> bool:
        if row.placed_at is None:
            return True
        return self._wall() - as_utc(row.placed_at) >= self._settle_after

    def settle_lost(self, session: Session, position_id: uuid.UUID) -> None:
        """Settle every stop of the position whose answer was lost (see `_settle`)."""
        if any(self.lost(row) for row in self.stops_of(session, position_id)):
            self._settled_stops(session, self._target(session, position_id))

    def _settled_stops(self, session: Session, target: _Target) -> list[HatOrder]:
        """The position's working stops, after settling those whose answer was lost."""
        rows = self.stops_of(session, target.position.id)
        lost = [row for row in rows if self.lost(row)]
        if not lost:
            return rows
        self._settle(session, target, lost)
        return self.stops_of(session, target.position.id)

    def _settle(self, session: Session, target: _Target, rows: list[HatOrder]) -> None:
        """Look the stops in `rows` (each sent, its answer lost) up at the broker once they are
        old enough: a GTT in the GTT book, a day stop by its tag. Found is adopted as placed;
        absent after a good read never landed and ends `expired`. An unreadable book leaves them
        as they are."""
        ready = [row for row in rows if self._settled_long_enough(row)]
        gtts = [row for row in ready if row.purpose == STOP_GTT]
        days = [row for row in ready if row.purpose != STOP_GTT]
        bot_id = target.signal.bot_id
        if gtts:
            try:
                book = self._broker.gtts()
            except BrokerUnavailableError:
                book = None
            if book is not None:
                claimed = self._claimed_gtts(session, target.account_id)
                for row in gtts:
                    found = find_gtt(book, row, target.symbol, claimed)
                    if found is None:
                        end_lost_order(session, row)
                    else:
                        adopt_gtt(session, row, found, bot_id)
                        claimed.add(found.gtt_id)
                    session.commit()
        if days:
            try:
                orders = {order.tag: order for order in self._broker.orders()}
            except BrokerUnavailableError:
                return
            for row in days:
                found_order = orders.get(row.tag)
                if found_order is None:
                    end_lost_order(session, row)
                else:
                    adopt_broker_order(session, row, found_order, bot_id)
                session.commit()

    @staticmethod
    def _claimed_gtts(session: Session, account_id: uuid.UUID) -> set[str]:
        """Broker GTT ids HAT's rows already hold (a lost GTT is never matched to one)."""
        return set(
            session.scalars(
                select(HatOrder.broker_order_id).where(
                    HatOrder.broker_account_id == account_id,
                    HatOrder.purpose == STOP_GTT,
                    HatOrder.broker_order_id.is_not(None),
                )
            )
        )

    # --- protect, resize, cancel --------------------------------------------------------------

    @_serialised
    def protect(self, session: Session, position_id: uuid.UUID, now: datetime) -> str:
        """After a fill: make the broker stop cover the open quantity (HAT-EXEC-060). A closed
        position's working stops are cancelled."""
        target = self._target(session, position_id)
        position = target.position
        if position.closed_at is not None or position.qty_open <= 0:
            self.cancel_all(session, position_id, now)
            if position.closed_at is None:
                self.cancel_entry(session, position_id)
            return CLOSED
        working = self.stops_of(session, position.id)
        if any(row.purpose == STOP_GTT and row.status == "partially_filled" for row in working):
            # The GTT triggered and its sale is part filled: the rest may still sell. The sync
            # protects what is left once that order ends (`FillSync._gtt_unfilled`).
            return PROTECTED
        if self.exit_working(session, position.id, LIVE):
            # A sale the broker holds: `make_room` shrank the stops to what it leaves, and a
            # stop over the shares it sells would be refused or hold them. The sync protects
            # what is left once the sale ends. A sale whose answer was lost does not count: the
            # position is protected meanwhile (HAT-EXEC-065).
            return EXIT_WORKING
        lost_gtts = [row for row in working if row.purpose == STOP_GTT and self.lost(row)]
        if lost_gtts:
            self._settle(session, target, lost_gtts)
            working = self.stops_of(session, position.id)
        covered = self._cover_live(session, target, working, now)
        if covered is not None:
            return PROTECTED if covered else FAILED  # a refused resize is alerted
        # A stop reserved and never sent is sent; a day stop whose answer was lost is looked up
        # by its tag and sent again only when absent, once it is old enough (still in flight
        # otherwise). A lost GTT that settling left is never sent again: the GTT book may lag one
        # that landed.
        ready = [
            row
            for row in working
            if row.status in UNSURE
            and (not self.lost(row) or (row.purpose == STOP_DAY and self._settled_long_enough(row)))
        ]
        if any(self._resend(session, target, row) for row in ready):
            return PROTECTED
        lost = [row for row in self.stops_of(session, position.id) if self.lost(row)]
        if lost:
            # A stop whose answer was lost may rest at the broker and could not be settled yet
            # (sent too recently, or the book unreadable): nothing is placed on top of it; the
            # next sync settles it. Only an answer the settle could not decide is alerted: a
            # young one is still in flight and alerting it on every sync would only add noise.
            if any(self._settled_long_enough(row) for row in lost):
                gtt = any(row.purpose == STOP_GTT for row in lost)
                reason = "gtt_unconfirmed" if gtt else "stop_unconfirmed"
                self._alert(ALERT_STOP_NOT_PLACED, target, now, reason)
            return FAILED
        purpose = STOP_DAY if self._day_stop_time(now) else STOP_GTT
        return PLACED if self._place(session, target, purpose, now) else FAILED

    def _cover_live(
        self, session: Session, target: _Target, working: list[HatOrder], now: datetime
    ) -> bool | None:
        """Resize every live broker stop to the open quantity: True when all now cover it,
        False when a resize failed, None when no stop is live."""
        live = [row for row in working if row.status in LIVE and row.broker_order_id]
        if not live:
            return None
        resized = True
        for row in live:
            if row.qty != target.position.qty_open and not self._resize(session, target, row, now):
                resized = False
        return resized

    def _resize(self, session: Session, target: _Target, row: HatOrder, now: datetime) -> bool:
        return self._resize_to(session, target, row, target.position.qty_open, now)

    def _resize_to(
        self, session: Session, target: _Target, row: HatOrder, qty: int, now: datetime
    ) -> bool:
        trigger = Decimal(row.trigger_price) if row.trigger_price is not None else None
        if trigger is None or row.broker_order_id is None:
            return False
        if row.purpose == STOP_DAY:
            ack = self._broker.modify_order(
                row.broker_order_id, order_type=OrderType.SL_M, qty=qty, trigger_price=trigger
            )
        else:
            tick = self._tick_for(session, target.position)
            if tick is None:
                self._alert(ALERT_STOP_NOT_MOVED, target, now, "no_tick")
                return False
            ack = self._broker.modify_gtt(
                GttModify(row.broker_order_id, qty, trigger, gtt_limit_price(trigger, tick))
            )
        if not ack.ok:
            self._alert(ALERT_STOP_NOT_MOVED, target, now, ack.code)
            return False
        row.qty = qty
        if row.purpose == STOP_GTT:
            self._order_event(session, row, target, EVENT_GTT_REFRESHED, now)
        session.commit()
        return True

    def cancel_all(self, session: Session, position_id: uuid.UUID, now: datetime) -> int:
        """Cancel the working stops of a position that has nothing left to protect (a stop
        resting under a sold position could sell shares that are not there). Returns how many
        the broker confirmed cancelled."""
        target = self._target(session, position_id)
        cancelled = 0
        for row in self._settled_stops(session, target):  # a lost stop that landed is cancelled
            if self._cancel(session, target, row, now):
                cancelled += 1
        return cancelled

    def _cancel(self, session: Session, target: _Target, row: HatOrder, now: datetime) -> bool:
        if row.status == "pending" and row.placed_at is None:
            row.status = "cancelled"  # never sent
            session.commit()
            return True
        if row.broker_order_id is None:
            return False  # outcome unknown: reconciliation and the verify see it
        if row.purpose == STOP_GTT:
            ack = self._broker.cancel_gtt(row.broker_order_id)
        else:
            ack = self._broker.cancel_order(row.broker_order_id)
        if not ack.ok:
            logger.warning("stop %s not cancelled (%s)", row.tag, ack.code)
            return False
        row.status = "cancelled"
        self._order_event(session, row, target, "order.cancelled", now)
        session.commit()
        return True

    def market_sale(self, broker_order_id: str) -> AckResult:
        """Turn the limit sale a triggered GTT left resting below its limit into a market sale
        (HAT-EXEC-066): the stop has triggered, so the shares go at the market instead of
        waiting unsold for a price that has gone. It sells only the shares that order already
        holds, so it can never sell more than the position."""
        return self._broker.modify_order(broker_order_id, order_type=OrderType.MARKET)

    def cancel_entry(self, session: Session, position_id: uuid.UUID) -> None:
        """Ask the broker to cancel the unfilled rest of the position's entry, once (again only
        after the broker could not be reached; a refusal means it filled or ended). The order's
        status is left to the broker's book (`FillSync`): a fill that lands before the cancel is
        recorded against a position that is still open. An entry of an earlier trade date
        expired at its close; one whose answer was lost has no broker id and ends at the close."""
        position = session.get(HatPosition, position_id)
        if position is None:
            return
        self._cancel_entries(session, position.signal_id, trade_date_of(self._wall()))

    def cancel_unfilled_entry(self, session: Session, signal_id: uuid.UUID, now: datetime) -> bool:
        """Cancel the signal's real entry while nothing of it has filled (PR 49): its break
        failed or it waited too long. An entry with a fill (a real position, or a part fill the
        broker reported) is left working: the partial-entry policy keeps the remainder until the
        close. The cancel is the one `cancel_entry` sends; the signal ends Cancelled when
        the fill sync sees the order end with nothing filled, and a fill that lands first opens
        the position as any fill does. True when the broker took a cancel."""
        filled = session.scalar(
            select(HatPosition.id).where(
                HatPosition.signal_id == signal_id, HatPosition.is_virtual.is_(False)
            )
        )
        part = session.scalar(
            select(HatOrder.id).where(
                HatOrder.signal_id == signal_id,
                HatOrder.purpose == ENTRY,
                HatOrder.status.in_(("partially_filled", "filled")),
            )
        )
        if filled is not None or part is not None:
            return False
        return self._cancel_entries(session, signal_id, trade_date_of(now))

    def _cancel_entries(self, session: Session, signal_id: uuid.UUID, today: date) -> bool:
        asked = False
        for row in session.scalars(
            select(HatOrder).where(
                HatOrder.signal_id == signal_id,
                HatOrder.purpose == ENTRY,
                HatOrder.is_virtual.is_(False),
                HatOrder.status.in_(LIVE),
                HatOrder.placed_at.is_not(None),
            )
        ):
            if (
                row.broker_order_id is None
                or row.id in self._entry_cancels
                or trade_date_of(as_utc(row.placed_at)) < today
            ):
                continue
            ack = self._broker.cancel_order(row.broker_order_id)
            if ack.ok or ack.code not in CODES_NOT_SENT:
                self._entry_cancels.add(row.id)
            if ack.ok:
                asked = True
            else:
                logger.warning("entry %s remainder not cancelled (%s)", row.tag, ack.code)
        return asked

    def make_room(self, session: Session, position_id: uuid.UUID, keep: int, now: datetime) -> bool:
        """Before a sale: shrink the position's resting stops to the `keep` shares that stay
        (cancel them when none stay), since a broker holds the shares a resting CNC sell covers
        and would refuse the sale; before a sale of every share the entry remainder is cancelled
        too. True when no stop covers more than `keep` shares; a stop that could not be changed
        is alerted and the sale waits. A failed sale is protected again by
        the next sync. A stop whose answer was lost is settled first; one still unknown is
        skipped (HAT-EXEC-065)."""
        target = self._target(session, position_id)
        if keep <= 0:
            self.cancel_entry(session, position_id)  # never holds the sale back
        rows = self._settled_stops(session, target)
        done = True
        for row in rows:
            if row.qty <= keep or self.lost(row):
                continue
            if keep <= 0 or row.status not in LIVE:
                done = self._cancel(session, target, row, now) and done
            else:
                done = self._resize_to(session, target, row, keep, now) and done
        return done

    # --- follow the stop up -------------------------------------------------------------------

    def follow(self, session: Session, position_id: uuid.UUID, now: datetime) -> bool:
        """Move every working broker stop up to the position's current stop (HAT-EXEC-064).
        A broker stop is never moved down. True when every working stop is at the target."""
        target = self._target(session, position_id)
        position = target.position
        if position.closed_at is not None or position.qty_open <= 0:
            return True
        tick = self._tick_for(session, position)
        goal = broker_trigger(Decimal(position.current_stop), tick)
        done = True
        triggered = self.triggered_gtts(session, position.id, now)
        for row in self.stops_of(session, position.id, *LIVE):
            if row.id in triggered:
                continue  # its sale is working; the fill sync sells it at the market
            current = Decimal(row.trigger_price) if row.trigger_price is not None else None
            if current is not None and goal <= current:
                if goal < current:
                    logger.error("stop %s would move down; left where it is", row.tag)
                continue
            done = self._move(session, target, row, goal, tick, now) and done
        return done

    def rescaled(self, session: Session, position_id: uuid.UUID, now: datetime) -> bool:
        """After a split or bonus rescaled the position (`position.rescaled`, HAT-EXEC-143): move
        every live broker stop resting above the rescaled stop down to it, keeping the quantity
        the broker stop already covers; the fill sync then resizes it to the rescaled quantity,
        so a refused resize (shares not yet credited) still leaves the old shares under the new
        stop. Waits while a sale is working. The only move down a broker stop makes, and only
        for a rescaled position. True when no stop is left above the position's stop."""
        target = self._target(session, position_id)
        position = target.position
        if position.closed_at is not None or position.qty_open <= 0:
            return True
        if not applied_factors(session, position.id):
            # Never rescaled: nothing to follow, but a pending action means the stop is not done.
            return not pending_actions(session, position, trade_date_of(now))
        if self.exit_working(session, position.id) or self.triggered_gtts(
            session, position.id, now
        ):
            return False  # the sale holds the shares; the next sync tries again
        tick = self._tick_for(session, position)
        goal = broker_trigger(Decimal(position.current_stop), tick)
        done = True
        for row in self.stops_of(session, position.id, *LIVE):
            if row.trigger_price is None or Decimal(row.trigger_price) <= goal:
                continue
            if row.purpose == STOP_GTT and tick is None:
                self._alert(ALERT_STOP_NOT_MOVED, target, now, "no_tick")
                done = False
                continue
            moved = self._move(session, target, row, goal, tick, now, qty=row.qty)
            done = moved and done
        return done

    def _move(
        self,
        session: Session,
        target: _Target,
        row: HatOrder,
        goal: Decimal,
        tick: Decimal | None,
        now: datetime,
        *,
        qty: int | None = None,
    ) -> bool:
        if row.broker_order_id is None:
            return False
        limit: Decimal | None = None
        if row.purpose == STOP_DAY:
            sized = {} if qty is None else {"qty": qty}  # a rescale keeps the stop's own size
            ack = self._broker.modify_order(
                row.broker_order_id, order_type=OrderType.SL_M, trigger_price=goal, **sized
            )
        else:
            if tick is None:
                self._alert(ALERT_STOP_NOT_MOVED, target, now, "no_tick")
                return False
            limit = gtt_limit_price(goal, tick)
            size = target.position.qty_open if qty is None else qty
            ack = self._broker.modify_gtt(GttModify(row.broker_order_id, size, goal, limit))
        if not ack.ok:
            self._alert(ALERT_STOP_NOT_MOVED, target, now, ack.code)
            return False
        row.trigger_price = goal
        if limit is not None:
            row.price = limit
            row.qty = size
            self._order_event(session, row, target, EVENT_GTT_REFRESHED, now)
        session.commit()
        return True

    # --- the 15:25 swap -----------------------------------------------------------------------

    @_serialised
    def swap(self, session: Session, position_id: uuid.UUID, now: datetime) -> str:
        """Replace the intraday stop with a GTT for the night (HAT-EXEC-061): the GTT first, the
        intraday stop cancelled only once the GTT is confirmed placed."""
        target = self._target(session, position_id)
        position = target.position
        if position.closed_at is not None or position.qty_open <= 0:
            return CLOSED
        if self.exit_working(session, position.id):
            return EXIT_WORKING  # the sale comes first; the job tries the swap again
        has_gtt = self._has_live_gtt(session, position.id)
        if not has_gtt:
            unsure = self._unsure_gtts(session, position.id)
            if any(self.lost(row) for row in unsure):
                self._settle(session, target, [row for row in unsure if self.lost(row)])
                has_gtt = self._has_live_gtt(session, position.id)
                unsure = self._unsure_gtts(session, position.id)
            has_gtt = has_gtt or any(self._resend(session, target, row) for row in unsure)
            if not has_gtt and unsure:
                self._alert(ALERT_GTT_SWAP_FAILED, target, now, "gtt_unconfirmed")
                return FAILED
        if not has_gtt and not self._place(session, target, STOP_GTT, now, ALERT_GTT_SWAP_FAILED):
            return FAILED  # the intraday stop keeps working; alerted
        # Every day stop is cancelled, even after one fails. A day stop not cancelled (refused,
        # or its outcome unknown) would sell the same shares as the GTT: the job fails and tries
        # the cancel again.
        cancelled = True
        for row in self.stops_of(session, position.id):
            if row.purpose == STOP_DAY and not self._cancel(session, target, row, now):
                cancelled = False
        return SWAPPED if cancelled else FAILED

    def _has_live_gtt(self, session: Session, position_id: uuid.UUID) -> bool:
        return any(
            row.purpose == STOP_GTT and row.broker_order_id
            for row in self.stops_of(session, position_id, *LIVE)
        )

    def _unsure_gtts(self, session: Session, position_id: uuid.UUID) -> list[HatOrder]:
        return [r for r in self.stops_of(session, position_id, *UNSURE) if r.purpose == STOP_GTT]

    # --- the nightly verify -------------------------------------------------------------------

    def _renewed(
        self, session: Session, target: _Target, row: HatOrder, goal: Decimal, now: datetime
    ) -> bool:
        """A GTT placed `GTT_RENEW_AFTER` ago or more is cancelled for the verify to place again
        at `goal` (research G24). True once the broker confirmed the cancel; a refused cancel
        leaves it resting and refreshed as any other, and the next night tries again. One whose
        new trigger would rest below the old (a coarser tick, F24) is not renewed."""
        # by the wall clock the placement was stamped with, as a settle reads it
        if row.placed_at is None or self._wall() - as_utc(row.placed_at) < GTT_RENEW_AFTER:
            return False
        if row.trigger_price is not None and goal < Decimal(row.trigger_price):
            return False
        return self._cancel(session, target, row, now)

    def _old_series(self, session: Session, target: _Target, row: HatOrder) -> bool:
        """The GTT went out under the old series' symbol of a position whose instrument changed
        series (HAT-EXEC-146): placed on or before the day the old row left the listing. That
        symbol takes no order, so a GTT the broker still lists under it protects nothing. The
        weekly refresh that delists runs on a Saturday, when no GTT is placed; one placed on that
        day is counted old, so at worst it is placed again once. With no Fyers symbol for the new
        series nothing could replace it, so it is left for the owner (the URGENT alert says
        `unmapped`)."""
        if target.unmapped or series_changed(session, target.position) is None:
            return False
        old = session.get(HatInstrument, target.position.instrument_id)
        if row.placed_at is None or old is None or old.delisted_on is None:
            return True
        return trade_date_of(as_utc(row.placed_at)) <= old.delisted_on

    @_serialised
    def check_gtt(
        self, session: Session, position_id: uuid.UUID, now: datetime, book: Collection[str]
    ) -> str:
        """Before the open around an ex-date or a series change (HAT-EXEC-145): `book` holds
        the ids of the GTTs the broker's GTT book shows (resting or triggered). A live GTT of the
        position the book does not hold was cancelled by the broker: it is ended `cancelled`,
        alerted (`gtt.missing`, code `gtt_cancelled_by_broker`) and the position protected again
        (`protect`: a GTT outside the session, an intraday stop inside it; a refused GTT is
        alerted `stop.not_placed` and the fill sync places the intraday stop from 09:15). A GTT
        whose answer was lost is settled first; one still unsure is alerted (`gtt.unverified`)
        and nothing is placed on top of it. A position still awaiting a split or bonus rescale
        gets nothing yet (UNVERIFIED, so the job tries again; `ca.rescale` alerts it): a GTT at
        its old-scale stop would rest above the ex-date price and sell at the open. A GTT still
        listed under the old series' symbol (HAT-EXEC-146) is cancelled and placed again under
        the new one; a refused cancel is alerted (`gtt.unverified`) and nothing is placed."""
        target = self._target(session, position_id)
        position = target.position
        if position.closed_at is not None or position.qty_open <= 0:
            return CLOSED
        gtts = [row for row in self.stops_of(session, position.id) if row.purpose == STOP_GTT]
        if any(self.lost(row) for row in gtts):
            self._settle(session, target, [row for row in gtts if self.lost(row)])
            gtts = [r for r in self.stops_of(session, position.id) if r.purpose == STOP_GTT]
        live = [row for row in gtts if row.status in LIVE and row.broker_order_id]
        stale = [row for row in live if self._old_series(session, target, row)]
        if any(row.broker_order_id in book for row in live if row not in stale):
            return VERIFIED
        if any(self.lost(row) for row in gtts):
            self._alert(ALERT_GTT_UNVERIFIED, target, now, "gtt_unconfirmed")
            return UNVERIFIED
        if pending_actions(session, position, trade_date_of(now)):
            return UNVERIFIED
        for row in stale:
            if row.broker_order_id in book and not self._cancel(session, target, row, now):
                self._alert(ALERT_GTT_UNVERIFIED, target, now, "series_cancel_refused")
                return UNVERIFIED
        live = [row for row in live if row.status in LIVE]  # the broker cancelled these
        if live:
            self._alert(ALERT_GTT_MISSING, target, now, "gtt_cancelled_by_broker")
            for row in live:
                row.status = "cancelled"
                self._order_event(session, row, target, "order.cancelled", now)
            session.commit()
        outcome = self.protect(session, position_id, now)
        if outcome == PLACED:
            return REPLACED
        return VERIFIED if outcome == PROTECTED else FAILED

    def unverified(
        self, session: Session, position_id: uuid.UUID, now: datetime, reason: str
    ) -> str:
        """The verify could not run for the position (its fills unread): `gtt.unverified` is
        alerted and its stops are left as they are."""
        target = self._target(session, position_id)
        self._alert(ALERT_GTT_UNVERIFIED, target, now, reason)
        return FAILED

    @_serialised
    def verify(self, session: Session, position_id: uuid.UUID, now: datetime) -> str:
        """Confirm the position's GTT rests at the broker at the current stop (HAT-EXEC-063,
        HAT-OPS-024): refreshed, else alerted and placed again."""
        target = self._target(session, position_id)
        position = target.position
        if position.closed_at is not None or position.qty_open <= 0:
            return CLOSED
        tick = self._tick_for(session, position)
        goal = broker_trigger(Decimal(position.current_stop), tick)
        gtts = [row for row in self.stops_of(session, position.id) if row.purpose == STOP_GTT]
        if any(self.lost(row) for row in gtts):
            self._settle(session, target, [row for row in gtts if self.lost(row)])
            gtts = [r for r in self.stops_of(session, position.id) if r.purpose == STOP_GTT]
        live = [
            (row, row.broker_order_id) for row in gtts if row.status in LIVE and row.broker_order_id
        ]
        if tick is None:
            # No GTT price can be set without the tick: told, and nothing is ended or placed.
            self._alert(ALERT_GTT_UNVERIFIED, target, now, "no_tick")
            return UNVERIFIED
        if (
            live
            and self._old_series(session, target, live[-1][0])
            and self._cancel(session, target, live[-1][0], now)
        ):
            # Placed under the old series' symbol, which takes no order (HAT-EXEC-146): placed
            # again under the new one; a refused cancel leaves it refreshed and alerted by the
            # series alert, and the next night tries again.
            return REPLACED if self._place(session, target, STOP_GTT, now) else FAILED
        if live and self._renewed(session, target, live[-1][0], goal, now):
            # The old GTT is cancelled (the market is shut at the nightly verify, so nothing can
            # trigger meanwhile) and placed again, with a new placement date.
            return REPLACED if self._place(session, target, STOP_GTT, now) else FAILED
        if live:
            row, gtt_id = live[-1]
            limit = gtt_limit_price(goal, tick)
            # A stop only moves up: a coarser tick that floors the stop below the resting trigger
            # leaves the trigger (and its limit) where they are (F24).
            keep = row.trigger_price is not None and goal < Decimal(row.trigger_price)
            if keep:
                goal = Decimal(row.trigger_price)
                limit = Decimal(row.price) if row.price is not None else gtt_limit_price(goal, tick)
            ack = self._broker.modify_gtt(GttModify(gtt_id, position.qty_open, goal, limit))
            if ack.ok:
                row.qty, row.trigger_price, row.price = position.qty_open, goal, limit
                self._order_event(session, row, target, EVENT_GTT_REFRESHED, now)
                session.commit()
                return VERIFIED
            # A GTT kept above the new tick's floor is never replaced by a lower one: told, and
            # left resting.
            if keep or ack.code in CODES_NOT_SENT or not self._cancel(session, target, row, now):
                # The old GTT may still rest at the broker: a second one could sell the same
                # shares twice. Told, and nothing is placed.
                self._alert(ALERT_GTT_UNVERIFIED, target, now, ack.code)
                return UNVERIFIED
        elif any(self.lost(row) for row in gtts):
            # A GTT whose answer was lost that the GTT book could not settle (unreadable, or
            # sent too recently) may rest at the broker: told, and nothing is placed.
            self._alert(ALERT_GTT_UNVERIFIED, target, now, "gtt_unconfirmed")
            return UNVERIFIED
        # No GTT rests at the broker (none, cancelled above, triggered or ended): alert and place
        # a new one. A row left open is ended first so it is never sent again.
        self._alert(ALERT_GTT_MISSING, target, now, "no_active_gtt")
        for row in gtts:
            if row.status in WORKING:
                row.status = "expired"
        session.commit()
        return REPLACED if self._place(session, target, STOP_GTT, now) else FAILED
