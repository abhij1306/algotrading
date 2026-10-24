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
  sale of all of them (`make_room` to 0), the unfilled rest of its entry is cancelled at the
  broker (`entry_cancel.EntryCancel`, which also cancels an entry nothing of which filled).
- **When the stop rises** (`follow`): the working stop is modified up to the new price (day:
  `modify_order`; GTT: `modify_gtt`, event `gtt.refreshed`), never down (`raise_only`), at the
  stop rounded down to the tick (`broker_trigger`).
- **The GTT upkeep** (`gtt_upkeep.GttUpkeep`): the 15:25 swap, the nightly verify and the
  check against the broker's GTT book, over the write steps below.
- **A series change** (`target`, HAT-EXEC-146): a position whose instrument moved to another
  series (EQ to BE) after it opened has its stops placed under the instrument's current symbol
  (`series.series_changed`).
- **A lost answer** (`settle_lost`, HAT-EXEC-065): once `settle_after` has passed since its
  send, a GTT is looked up in the GTT book (`submit.find_gtt`), a day stop by its tag. Found, it
  is adopted; found with shares sold, it waits for the fill sync to adopt it with its fills
  (`position_desk.found_verdict`), so no stop goes over shares sold; absent after a good read,
  it ends `expired` and the position is protected again. While a stop's outcome is unknown it
  never holds a sale back (`make_room` skips it; a resting CNC stop makes the broker refuse the
  sale, the safe way round).

The pure planners in `stop_plans` (`protect_plan`, `verify_plan`, `raise_only`) decide; `StopDesk`
reads the rows, carries a step out and plans again when the broker leaves it open. Its write
steps (`target`, `settled_stops`, `place`, `resend`, `cancel`, `alert`, `order_event`) are the
ones `GttUpkeep` uses; every caller holds the position's lock (`position_desk.serialised`).

Every order goes through `submit.reserve_order` and `submit.send_order` (trading-safety rule 1);
modifies and cancels go straight to the `HatBroker` contract with the broker id `send_order`
stored. Nothing here checks a kill switch: stops are never blocked. Alerts carry fixed
codes only, never broker text.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..engine.rescale import applied_factors, pending_actions
from ..errors import HatError
from ..events import record
from ..models import HatBot, HatEvent, HatInstrument, HatOrder, HatPosition, HatSignal
from ..notify import LogNotifier, Notifier
from ..risk.sessions import SessionCalendar, SessionState
from ..time import IST, day_bounds, trade_date_of, utc_now
from .broker import CODE_UNAVAILABLE as BROKER_UNAVAILABLE
from .broker import (
    AckResult,
    BrokerUnavailableError,
    GttModify,
    HatBroker,
    OrderType,
    Side,
)
from .entry_cancel import EntryCancel
from .gtt import gtt_limit_price
from .guard import ExecutionGuard
from .order_book import EXIT_SALES, adopt_broker_order, adopt_gtt, end_lost_order, find_gtt
from .position_desk import PositionDesk, Verdict, serialised
from .series import broker_symbol, series_changed
from .stop_plans import (
    LIVE,
    STOP_DAY,
    STOP_GTT,
    WORKING,
    ProtectPlan,
    ProtectStep,
    broker_trigger,
    lost,
    protect_plan,
    raise_only,
    retry_due,
)
from .submit import (
    AwaitingFillsError,
    OrderRefusedError,
    OrderRequest,
    OrderSender,
    SendOutcome,
    TickSource,
    UnresolvedOrderError,
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
    "ALERT_STOP_WOULD_MOVE_DOWN",
    "SWAP_AT",
    "AlertedToday",
    "StopDesk",
]

logger = logging.getLogger(__name__)

ENTRY = "entry"

EVENT_GTT_REFRESHED = "gtt.refreshed"
EVENT_GTT_TRIGGERED = "gtt.triggered"
MAX_STOP_REJECTIONS = 2  # the original and one retry (HAT-OPS-032)
SWAP_AT = time(15, 25)  # IST: from here on a new stop is a GTT (HAT-EXEC-061)
CODE_SERIES_UNMAPPED = "series_unmapped"  # the new series has no Fyers symbol
NO_TICK = "no_tick"  # the instrument's tick is unknown: a GTT has no limit price
# a lost stop found in the book with shares sold: the fill sync records its fills
CODE_AWAITING_FILLS = "awaiting_fills"

ALERT_STOP_NOT_PLACED = "stop.not_placed"
ALERT_EXIT_NOT_PLACED = "exit.not_placed"
ALERT_STOP_NOT_MOVED = "stop.not_moved"
# the position's stop is below the resting broker stop, which only rises: left where it is (QA15)
ALERT_STOP_WOULD_MOVE_DOWN = "stop.would_move_down"
ALERT_GTT_SWAP_FAILED = "gtt.swap_failed"
ALERT_GTT_MISSING = "gtt.missing"
ALERT_GTT_UNVERIFIED = "gtt.unverified"
ALERT_GTT_UNFILLED = "gtt.unfilled"
# Raised again at every sync while the cause stands: told once per position, reason and trade
# date (C22 A4).
ONCE_A_DAY = frozenset({ALERT_STOP_NOT_PLACED, ALERT_STOP_NOT_MOVED, ALERT_STOP_WOULD_MOVE_DOWN})

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


@dataclass(frozen=True)
class StopTarget:
    """What a position's stop order is placed for."""

    position: HatPosition
    signal: HatSignal
    account_id: uuid.UUID
    symbol: str
    # The series changed and the new series has no stored Fyers symbol (a BZ stock): the
    # adapter would fall back to the old `-EQ` symbol, so no stop is sent (HAT-EXEC-146).
    unmapped: bool = False


class AlertedToday:
    """The (code, position, reason) alerts already raised on the current trade date; a new
    trade date starts empty. Thread-safe: the desk's callers hold different positions' locks."""

    def __init__(self) -> None:
        self._day: date | None = None
        self._seen: set[tuple[str, uuid.UUID, str]] = set()
        self._lock = threading.Lock()

    def first(self, day: date, code: str, position_id: uuid.UUID, reason: str) -> bool:
        """True the first time this alert is asked for on `day` (and records it)."""
        key = (code, position_id, reason)
        with self._lock:
            if day != self._day:
                self._day, self._seen = day, set()
            if key in self._seen:
                return False
            self._seen.add(key)
            return True

    def forget(self, day: date, code: str, position_id: uuid.UUID, reason: str) -> None:
        """Ask for the alert again on `day`: its delivery failed."""
        with self._lock:
            if day == self._day:
                self._seen.discard((code, position_id, reason))


class StopDesk:
    """Broker stops for one broker account's session; `guard` is the account's order gate (it
    never holds a stop back: the reconciliation gates entries only, D118)."""

    def __init__(
        self,
        broker: HatBroker,
        guard: ExecutionGuard,
        *,
        positions: PositionDesk,
        tick_for: TickSource,
        calendar: SessionCalendar | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self._broker = broker
        self._guard = guard
        # The position locks, the settle wait and the clock that stamps the sends.
        self._positions = positions
        self._clock = positions.clock
        self._tick_for = tick_for
        self._sender = OrderSender(broker, guard, self._clock, tick_for)
        self._calendar = calendar
        self._notifier = notifier or LogNotifier()
        self._alerted = AlertedToday()  # the ONCE_A_DAY alerts raised
        self.entries = EntryCancel(broker)  # the entry remainders it cancels

    @property
    def positions(self) -> PositionDesk:
        """The account's position locks and settle wait, shared with its `TradeManager`."""
        return self._positions

    @property
    def broker(self) -> HatBroker:
        """The account's broker, for the GTT upkeep's modifies (`gtt_upkeep`)."""
        return self._broker

    def tick(self, session: Session, position: HatPosition) -> Decimal | None:
        """The instrument's tick for the position's stop prices."""
        return self._tick_for(session, position)

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

    def target(self, session: Session, position_id: uuid.UUID) -> StopTarget:
        position = session.get(HatPosition, position_id)
        if position is None or position.is_virtual:
            raise HatError("not a real position")
        session.refresh(position)
        signal = session.get(HatSignal, position.signal_id)
        bot = session.get(HatBot, position.bot_id)
        # The instrument's current symbol: after a series change (HAT-EXEC-146) the adapter maps
        # it to the new series' Fyers symbol, as the old series' symbol takes no order.
        symbol = session.scalar(
            select(HatInstrument.symbol).where(HatInstrument.id == position.instrument_id)
        )
        if signal is None or bot is None or symbol is None:
            raise HatError("position without signal, bot or instrument")
        moved = series_changed(session, position)
        unmapped = moved is not None and broker_symbol(session, moved.id) is None
        return StopTarget(position, signal, bot.broker_account_id, symbol, unmapped)

    # --- events and alerts --------------------------------------------------------------------

    @staticmethod
    def order_event(
        session: Session,
        row: HatOrder,
        target: StopTarget,
        event_type: str,
        now: datetime,
        reason: str | None = None,
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
                **({} if reason is None else {"reason": reason}),
            },
            correlation_id=target.signal.id,
            bot_id=target.signal.bot_id,
            occurred_at=now,
        )

    def alert(self, code: str, target: StopTarget, now: datetime, reason: str) -> None:
        """Tell the owner; a `ONCE_A_DAY` alert only the first time on the trade date for its
        position and reason (the next syncs try again and would raise it each time); one that
        could not be delivered is told again at the next sync."""
        day = trade_date_of(now)
        if code in ONCE_A_DAY and not self._alerted.first(day, code, target.position.id, reason):
            logger.info("%s of position %s (%s) already alerted", code, target.position.id, reason)
            return
        delivered = False
        try:
            delivered = self._notifier.alert(
                code,
                day,
                {
                    "bot_id": str(target.signal.bot_id),
                    "signal_id": str(target.signal.id),
                    "position_id": str(target.position.id),
                    "code": reason,
                },
            )
        finally:
            if not delivered and code in ONCE_A_DAY:
                self._alerted.forget(day, code, target.position.id, reason)

    # --- placing ------------------------------------------------------------------------------

    def _request(self, target: StopTarget, purpose: str, tick: Decimal | None) -> OrderRequest:
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
            position_id=position.id,
            price=gtt_limit_price(trigger, tick) if gtt and tick is not None else None,
            trigger_price=trigger,
        )

    def _send(self, session: Session, target: StopTarget, tag: str) -> SendOutcome | str:
        """One send of a reserved stop: the outcome, or a refusal code (nothing was sent, or it
        cannot be told)."""
        if target.unmapped:
            self._release_unsent(session, tag)
            return CODE_SERIES_UNMAPPED
        try:
            return send_order(session, self._sender, tag)
        except OrderRefusedError as exc:
            self._release_unsent(session, tag)
            return exc.code
        except BrokerUnavailableError:
            self._release_unsent(session, tag)
            return BROKER_UNAVAILABLE
        except AwaitingFillsError:
            return CODE_AWAITING_FILLS
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

    def place(
        self,
        session: Session,
        target: StopTarget,
        purpose: str,
        now: datetime,
        alert: str = ALERT_STOP_NOT_PLACED,
    ) -> bool:
        """Place a new stop of `purpose` for the open quantity; a rejection is retried once with
        the next tag. True once the broker confirmed it; an unconfirmed send is left to the next
        sync (a day stop is looked up by its tag; a GTT waits for the nightly verify). A failure
        raises `alert`. What is refused without the broker spends no tag, and a purpose the
        broker rejected is placed again at most once a minute (`retry_due`)."""
        if target.unmapped:
            self.alert(alert, target, now, CODE_SERIES_UNMAPPED)  # no tag spent
            return False
        tick = self._tick_for(session, target.position)
        if purpose == STOP_GTT and tick is None:
            self.alert(alert, target, now, NO_TICK)  # no limit price: no tag spent
            return False
        if not retry_due(self._last_rejected(session, target.position.id, purpose), now):
            return False  # alerted at the rejection
        reason = "broker_rejected"
        for _ in range(MAX_STOP_REJECTIONS):
            try:
                tag = reserve_order(session, self._request(target, purpose, tick))
            except HatError as exc:  # no limit price, the 37th tag, a failed reservation
                session.rollback()
                self.alert(alert, target, now, getattr(exc, "code", "reservation_failed"))
                return False
            sent = self._send(session, target, tag)
            if isinstance(sent, str):
                self.alert(alert, target, now, sent)
                return False
            if sent.status in LIVE:
                return True
            if sent.status != "rejected":
                self.alert(alert, target, now, sent.code)  # unconfirmed: told, left to sync
                return False
            reason = sent.code
        self.alert(alert, target, now, reason)
        return False

    @staticmethod
    def _last_rejected(session: Session, position_id: uuid.UUID, purpose: str) -> datetime | None:
        """When the position's latest stop of `purpose` the broker rejected was sent."""
        return session.scalar(
            select(func.max(HatOrder.placed_at)).where(
                HatOrder.position_id == position_id,
                HatOrder.purpose == purpose,
                HatOrder.status == "rejected",
            )
        )

    def resend(self, session: Session, target: StopTarget, row: HatOrder) -> bool:
        """Send a stop reserved earlier whose outcome is not known yet (by its own tag)."""
        sent = self._resent(session, target, row)
        return not isinstance(sent, str) and sent.status in LIVE

    def _resent(self, session: Session, target: StopTarget, row: HatOrder) -> SendOutcome | str:
        return self._send(session, target, row.tag)

    # --- lost answers (HAT-EXEC-065) ---------------------------------------------------------

    @serialised
    def settle_lost(self, session: Session, position_id: uuid.UUID) -> None:
        """Settle every stop of the position whose answer was lost (`_settled_stops`)."""
        if any(lost(row) for row in self.stops_of(session, position_id)):
            self.settled_stops(session, self.target(session, position_id))

    def settled_stops(
        self, session: Session, target: StopTarget, purpose: str | None = None
    ) -> list[HatOrder]:
        """The position's working stops, after settling those whose answer was lost (of
        `purpose` only, when given)."""
        rows = self.stops_of(session, target.position.id)
        unsure = [row for row in rows if lost(row) and purpose in (None, row.purpose)]
        if not unsure:
            return rows
        ready = [row for row in unsure if not self._positions.young(row.placed_at)]
        gtts = [row for row in ready if row.purpose == STOP_GTT]
        if gtts:
            self._settle_gtts(session, target, gtts)
        days = [row for row in ready if row.purpose != STOP_GTT]
        if days:
            self._settle_days(session, target, days)
        return self.stops_of(session, target.position.id)

    def _settle_gtts(self, session: Session, target: StopTarget, rows: list[HatOrder]) -> None:
        """Lost GTTs old enough against the broker's GTT book (`find_gtt`, `settle_verdict`):
        found is adopted as placed; absent after a good read never landed and ends `expired`. An
        unreadable book leaves them as they are."""
        try:
            book = self._broker.gtts()
        except BrokerUnavailableError:
            return
        claimed = self._claimed_gtts(session, target.account_id)
        for row in rows:
            found = find_gtt(book, row, target.symbol, claimed)
            verdict = self._positions.verdict(row.placed_at, found is not None)
            if found is not None and verdict is Verdict.ADOPT:
                adopt_gtt(session, row, found, target.signal.bot_id, utc_now(self._clock))
                claimed.add(found.gtt_id)
            elif verdict is Verdict.EXPIRE:
                end_lost_order(session, row)
            session.commit()

    def _settle_days(self, session: Session, target: StopTarget, rows: list[HatOrder]) -> None:
        """Lost day stops old enough against the broker's order book by their tags
        (`settle_verdict`): found is adopted; found with shares sold waits for the fill sync,
        which adopts it with its fills; absent after a good read never landed and ends
        `expired`. An unreadable book leaves them as they are."""
        try:
            orders = {order.tag: order for order in self._broker.orders()}
        except BrokerUnavailableError:
            return
        for row in rows:
            found = orders.get(row.tag)
            filled = 0 if found is None else found.filled_qty
            verdict = self._positions.verdict(row.placed_at, found is not None, filled_qty=filled)
            if found is not None and verdict is Verdict.ADOPT:
                adopt_broker_order(session, row, found, target.signal.bot_id, clock=self._clock)
            elif verdict is Verdict.EXPIRE:
                end_lost_order(session, row)
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

    @serialised
    def protect(self, session: Session, position_id: uuid.UUID, now: datetime) -> str:
        """After a fill: make the broker stop cover the open quantity (HAT-EXEC-060), one step
        of `protect_plan` at a time. A closed position's working stops are cancelled."""
        target = self.target(session, position_id)
        position = target.position
        open_qty = 0 if position.closed_at is not None else position.qty_open
        exit_live = open_qty > 0 and self.exit_working(session, position.id, LIVE)
        rows = self.stops_of(session, position.id)

        def plan(after: ProtectStep | None = None) -> ProtectPlan:
            return protect_plan(
                rows,
                open_qty,
                exit_live=exit_live,
                day_stop=self._day_stop_time(now),
                young=self._positions.young,
                after=after,
            )

        step = plan()
        if step.step is ProtectStep.SETTLE:
            rows = self.settled_stops(session, target, STOP_GTT)
            step = plan(ProtectStep.SETTLE)
        if step.step is ProtectStep.RESEND:
            filled = False
            for row in step.rows:
                sent = self._resent(session, target, row)
                if not isinstance(sent, str) and sent.status in LIVE:
                    return PROTECTED
                filled = filled or sent == CODE_AWAITING_FILLS
            rows = self.stops_of(session, position.id)
            step = plan(ProtectStep.RESEND)
            if filled and step.step is ProtectStep.WAIT_LOST:
                # The lost stop rested and sold: not a stop that failed, so nothing is alerted;
                # the fill sync records its fills and protects what is left.
                step = ProtectPlan(ProtectStep.WAIT_LOST)
        return self._carry_out(session, target, step, now)

    def _carry_out(
        self, session: Session, target: StopTarget, plan: ProtectPlan, now: datetime
    ) -> str:
        """The outcome of a `protect_plan` step that needs no further plan."""
        match plan.step:
            case ProtectStep.CLOSED:
                self.cancel_all(session, target.position.id, now)
                if target.position.closed_at is None:
                    self.cancel_entry(session, target.position.id)
                return CLOSED
            case ProtectStep.GTT_SELLING | ProtectStep.COVERED:
                return PROTECTED  # a selling GTT's rest: `FillSync._gtt_unfilled`
            case ProtectStep.EXIT_WORKING:
                return EXIT_WORKING  # `make_room` sized the stops; the sync protects the rest
            case ProtectStep.RESIZE:
                ok = True
                for row in plan.rows:  # every row is resized, even after a refusal
                    ok = self._resize(session, target, row, now) and ok
                return PROTECTED if ok else FAILED  # a refused resize is alerted
            case ProtectStep.WAIT_LOST:
                # A stop whose answer was lost may rest at the broker and could not be settled
                # yet: nothing is placed on top of it; the next sync settles it.
                if plan.reason is not None:
                    self.alert(ALERT_STOP_NOT_PLACED, target, now, plan.reason)
                return FAILED
            case ProtectStep.PLACE_DAY | ProtectStep.PLACE_GTT:
                purpose = STOP_DAY if plan.step is ProtectStep.PLACE_DAY else STOP_GTT
                return PLACED if self.place(session, target, purpose, now) else FAILED
            case _:
                raise AssertionError(f"protect step {plan.step} not carried out")

    def _resize(self, session: Session, target: StopTarget, row: HatOrder, now: datetime) -> bool:
        return self._resize_to(session, target, row, target.position.qty_open, now)

    def _resize_to(
        self, session: Session, target: StopTarget, row: HatOrder, qty: int, now: datetime
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
                self.alert(ALERT_STOP_NOT_MOVED, target, now, NO_TICK)
                return False
            ack = self._broker.modify_gtt(
                GttModify(row.broker_order_id, qty, trigger, gtt_limit_price(trigger, tick))
            )
        if not ack.ok:
            self.alert(ALERT_STOP_NOT_MOVED, target, now, ack.code)
            return False
        row.qty = qty
        if row.purpose == STOP_GTT:
            self.order_event(session, row, target, EVENT_GTT_REFRESHED, now)
        session.commit()
        return True

    @serialised
    def cancel_all(self, session: Session, position_id: uuid.UUID, now: datetime) -> int:
        """Cancel the working stops of a position that has nothing left to protect (a stop
        resting under a sold position could sell shares that are not there). Returns how many
        the broker confirmed cancelled."""
        target = self.target(session, position_id)
        cancelled = 0
        for row in self.settled_stops(session, target):  # a lost stop that landed is cancelled
            if self.cancel(session, target, row, now):
                cancelled += 1
        return cancelled

    def cancel(self, session: Session, target: StopTarget, row: HatOrder, now: datetime) -> bool:
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
        self.order_event(session, row, target, "order.cancelled", now)
        session.commit()
        return True

    def market_sale(self, broker_order_id: str) -> AckResult:
        """Turn the limit sale a triggered GTT left resting below its limit into a market sale
        (HAT-EXEC-066): the stop has triggered, so the shares go at the market instead of
        waiting unsold for a price that has gone. It sells only the shares that order already
        holds, so it can never sell more than the position."""
        return self._broker.modify_order(broker_order_id, order_type=OrderType.MARKET)

    @serialised
    def cancel_entry(self, session: Session, position_id: uuid.UUID) -> None:
        """Ask the broker to cancel the unfilled rest of the position's entry
        (`EntryCancel.cancel_rest`): a fill that lands before the cancel is recorded against a
        position that is still open."""
        position = session.get(HatPosition, position_id)
        if position is None:
            return
        self.entries.cancel_rest(session, position.signal_id, trade_date_of(self._clock.now()))

    @serialised
    def make_room(self, session: Session, position_id: uuid.UUID, keep: int, now: datetime) -> bool:
        """Before a sale: shrink the position's resting stops to the `keep` shares that stay
        (cancel them when none stay), since a broker holds the shares a resting CNC sell covers
        and would refuse the sale; before a sale of every share the entry remainder is cancelled
        too. True when no stop covers more than `keep` shares; a stop that could not be changed
        is alerted and the sale waits. A failed sale is protected again by
        the next sync. A stop whose answer was lost is settled first; one still unknown is
        skipped (HAT-EXEC-065)."""
        target = self.target(session, position_id)
        if keep <= 0:
            self.cancel_entry(session, position_id)  # never holds the sale back
        rows = self.settled_stops(session, target)
        done = True
        for row in rows:
            if row.qty <= keep or lost(row):
                continue
            if keep <= 0 or row.status not in LIVE:
                done = self.cancel(session, target, row, now) and done
            else:
                done = self._resize_to(session, target, row, keep, now) and done
        return done

    # --- follow the stop up -------------------------------------------------------------------

    @serialised
    def follow(self, session: Session, position_id: uuid.UUID, now: datetime) -> bool:
        """Move every working broker stop up to the position's current stop (HAT-EXEC-064).
        A broker stop is never moved down (`raise_only`). True when every working stop is at the
        target."""
        target = self.target(session, position_id)
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
            done = self._move(session, target, row, goal, tick, now) and done
        return done

    @serialised
    def rescaled(self, session: Session, position_id: uuid.UUID, now: datetime) -> bool:
        """After a split or bonus rescaled the position (`position.rescaled`, HAT-EXEC-143): move
        every live broker stop resting above the rescaled stop down to it, keeping the quantity
        the broker stop already covers; the fill sync then resizes it to the rescaled quantity,
        so a refused resize (shares not yet credited) still leaves the old shares under the new
        stop. Waits while a sale is working. The only move down a broker stop makes
        (`raise_only(rescaled=True)`), and only for a rescaled position. True when no stop is left
        above the position's stop."""
        target = self.target(session, position_id)
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
            done = self._move(session, target, row, goal, tick, now, rescaled=True) and done
        return done

    def _move(
        self,
        session: Session,
        target: StopTarget,
        row: HatOrder,
        goal: Decimal,
        tick: Decimal | None,
        now: datetime,
        *,
        rescaled: bool = False,
    ) -> bool:
        """Move the resting stop `row` toward `goal` as far as `raise_only` lets it (a rescale
        keeps the quantity the stop already covers). True when it rests where it should."""
        current = Decimal(row.trigger_price) if row.trigger_price is not None else None
        trigger = raise_only(current, goal, rescaled=rescaled)
        if trigger is None or trigger == current:
            if not rescaled and current is not None and goal < current:
                logger.error("stop %s would move down; left where it is", row.tag)
                self.alert(ALERT_STOP_WOULD_MOVE_DOWN, target, now, "raise_only")
            return True
        if row.broker_order_id is None:
            return False
        if row.purpose == STOP_DAY:
            refused = self._modify_day(row, trigger, rescaled=rescaled)
        else:
            refused = self._modify_gtt(session, target, row, trigger, tick, now, rescaled=rescaled)
        if refused is not None:
            self.alert(ALERT_STOP_NOT_MOVED, target, now, refused)
            return False
        row.trigger_price = trigger
        session.commit()
        return True

    def _modify_day(self, row: HatOrder, trigger: Decimal, *, rescaled: bool) -> str | None:
        """Modify the day stop `row` to `trigger` (a rescale sends its quantity too). The code
        of a refusal; None when the broker took it."""
        sized = {"qty": row.qty} if rescaled else {}
        ack = self._broker.modify_order(
            str(row.broker_order_id), order_type=OrderType.SL_M, trigger_price=trigger, **sized
        )
        return None if ack.ok else ack.code

    def _modify_gtt(
        self,
        session: Session,
        target: StopTarget,
        row: HatOrder,
        trigger: Decimal,
        tick: Decimal | None,
        now: datetime,
        *,
        rescaled: bool,
    ) -> str | None:
        """Modify the GTT `row` to `trigger` for the open quantity (a rescale keeps the quantity
        it covers); its limit and quantity are recorded once the broker took it. The code of a
        refusal; None when the broker took it."""
        if tick is None:
            return NO_TICK
        limit = gtt_limit_price(trigger, tick)
        size = row.qty if rescaled else target.position.qty_open
        ack = self._broker.modify_gtt(GttModify(str(row.broker_order_id), size, trigger, limit))
        if not ack.ok:
            return ack.code
        row.trigger_price = trigger
        row.price = limit
        row.qty = size
        self.order_event(session, row, target, EVENT_GTT_REFRESHED, now)
        return None
