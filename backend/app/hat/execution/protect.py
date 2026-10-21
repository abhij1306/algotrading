"""Protective stops at the broker for real positions (07 §6; HAT-EXEC-060..064, HAT-DEC-B17,
HAT-OPS-024; the model is `verification/tla/StopPlacement.tla`).

`StopDesk` keeps one resting broker stop under every open real position:

- **On a fill** (`protect`): with no working stop, an intraday stop order (purpose `stop_day`,
  SL-M SELL CNC for the open quantity at the position's stop) while the market is open and
  before the 15:25 swap, a GTT stop (`stop_gtt`) otherwise (a day stop placed after the swap
  would expire at the close with no GTT behind it); a working stop for another quantity is
  resized. A rejected stop is retried once with the next tag, then alerted (`stop.not_placed`);
  the next sync tries again. A closed position's working stops are cancelled.
- **When the stop rises** (`follow`): the working stop is modified up to the new price (day:
  `modify_order`; GTT: `modify_gtt`, event `gtt.refreshed`), never down. The broker trigger is
  the stop rounded down to the instrument's tick, so it is never above `current_stop`.
- **At ~15:25** (`swap`): the GTT is placed first and the intraday stop is cancelled only once
  the GTT is confirmed placed; a GTT that fails leaves the intraday stop working and raises
  `gtt.swap_failed`. A position with a working exit order is not swapped (its exit comes first).
- **Nightly** (`verify`): every open real position's GTT is refreshed to the current stop
  (`gtt.refreshed`). A GTT the broker no longer knows (triggered, cancelled or never confirmed)
  is alerted (`gtt.missing`) and placed again; when the broker cannot be reached the position
  is alerted (`gtt.unverified`) and nothing is placed, since the old GTT may still rest there.

Every order goes through `submit.reserve_order` and `submit.send_order` (trading-safety rule 1);
modifies and cancels go straight to the `HatBroker` contract with the broker id `send_order`
stored. Nothing here checks a kill switch: stops are never blocked (06 §6). Alerts carry fixed
codes only, never broker text.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time
from decimal import ROUND_FLOOR, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import HatError
from ..events import record
from ..models import HatBot, HatInstrument, HatOrder, HatPosition, HatSignal
from ..notify import LogNotifier, Notifier
from ..risk.sessions import SessionCalendar, SessionState
from ..time import IST, trade_date_of
from .broker import CODE_UNAVAILABLE as BROKER_UNAVAILABLE
from .broker import BrokerUnavailableError, GttModify, HatBroker, OrderType, Side
from .gtt import gtt_limit_price
from .guard import ExecutionGuard
from .submit import (
    OrderRefusedError,
    OrderRequest,
    SendOutcome,
    UnresolvedOrderError,
    reserve_order,
    send_order,
)

__all__ = [
    "ALERT_GTT_MISSING",
    "ALERT_GTT_SWAP_FAILED",
    "ALERT_GTT_UNFILLED",
    "ALERT_GTT_UNVERIFIED",
    "ALERT_STOP_NOT_MOVED",
    "ALERT_STOP_NOT_PLACED",
    "LIVE",
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

STOP_DAY = "stop_day"
STOP_GTT = "stop_gtt"
EXIT_SALES = ("exit_partial", "exit_full", "flatten")
EVENT_GTT_REFRESHED = "gtt.refreshed"
WORKING = ("pending", "unconfirmed", "placed", "partially_filled")
LIVE = ("placed", "partially_filled")
UNSURE = ("pending", "unconfirmed")
MAX_STOP_REJECTIONS = 2  # the original and one retry (HAT-OPS-032)
SWAP_AT = time(15, 25)  # IST: from here on a new stop is a GTT (HAT-EXEC-061)

ALERT_STOP_NOT_PLACED = "stop.not_placed"
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


class StopDesk:
    """Broker stops for one broker account's session; `guard` is that session's reconciliation
    state (nothing is placed before it reconciled)."""

    def __init__(
        self,
        broker: HatBroker,
        guard: ExecutionGuard,
        *,
        tick_for: TickSource,
        calendar: SessionCalendar | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self._broker = broker
        self._guard = guard
        self._tick_for = tick_for
        self._calendar = calendar
        self._notifier = notifier or LogNotifier()

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
    def exit_working(session: Session, position_id: uuid.UUID) -> bool:
        """A sale of the position may still be at the broker."""
        found = session.scalar(
            select(HatOrder.id).where(
                HatOrder.position_id == position_id,
                HatOrder.is_virtual.is_(False),
                HatOrder.purpose.in_(EXIT_SALES),
                HatOrder.status.in_(WORKING),
            )
        )
        return found is not None

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
        symbol = session.scalar(
            select(HatInstrument.symbol).where(HatInstrument.id == position.instrument_id)
        )
        if signal is None or bot is None or symbol is None:
            raise HatError("position without signal, bot or instrument")
        return _Target(position, signal, bot.broker_account_id, symbol)

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

    # --- protect, resize, cancel --------------------------------------------------------------

    def protect(self, session: Session, position_id: uuid.UUID, now: datetime) -> str:
        """After a fill: make the broker stop cover the open quantity (HAT-EXEC-060). A closed
        position's working stops are cancelled."""
        target = self._target(session, position_id)
        position = target.position
        if position.closed_at is not None or position.qty_open <= 0:
            self.cancel_all(session, position_id, now)
            return CLOSED
        working = self.stops_of(session, position.id)
        if any(row.purpose == STOP_GTT and row.status == "partially_filled" for row in working):
            # The GTT triggered and its sale is part filled: the rest may still sell. The sync
            # protects what is left once that order ends (`FillSync._gtt_unfilled`).
            return PROTECTED
        covered = self._cover_live(session, target, working, now)
        if covered is not None:
            return PROTECTED if covered else FAILED  # a refused resize is alerted
        unsure = [row for row in working if row.status in UNSURE]
        if any(self._resend(session, target, row) for row in unsure):
            return PROTECTED
        if any(row.status == "unconfirmed" and row.purpose == STOP_GTT for row in unsure):
            # A GTT whose answer was lost may rest at the broker: the nightly verify decides.
            self._alert(ALERT_STOP_NOT_PLACED, target, now, "gtt_unconfirmed")
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
        for row in self.stops_of(session, position_id):
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

    def make_room(self, session: Session, position_id: uuid.UUID, keep: int, now: datetime) -> bool:
        """Before a sale: shrink the position's resting stops to the `keep` shares that stay
        (cancel them when none stay), since a broker holds the shares a resting CNC sell covers
        and would refuse the sale. True when no stop covers more than `keep` shares; a stop that
        could not be changed is alerted and the sale waits. A failed sale is protected again by
        the next sync."""
        target = self._target(session, position_id)
        done = True
        for row in self.stops_of(session, position_id):
            if row.qty <= keep:
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
        for row in self.stops_of(session, position.id, *LIVE):
            current = Decimal(row.trigger_price) if row.trigger_price is not None else None
            if current is not None and goal <= current:
                if goal < current:
                    logger.error("stop %s would move down; left where it is", row.tag)
                continue
            done = self._move(session, target, row, goal, tick, now) and done
        return done

    def _move(
        self,
        session: Session,
        target: _Target,
        row: HatOrder,
        goal: Decimal,
        tick: Decimal | None,
        now: datetime,
    ) -> bool:
        if row.broker_order_id is None:
            return False
        limit: Decimal | None = None
        if row.purpose == STOP_DAY:
            ack = self._broker.modify_order(
                row.broker_order_id, order_type=OrderType.SL_M, trigger_price=goal
            )
        else:
            if tick is None:
                self._alert(ALERT_STOP_NOT_MOVED, target, now, "no_tick")
                return False
            limit = gtt_limit_price(goal, tick)
            ack = self._broker.modify_gtt(
                GttModify(row.broker_order_id, target.position.qty_open, goal, limit)
            )
        if not ack.ok:
            self._alert(ALERT_STOP_NOT_MOVED, target, now, ack.code)
            return False
        row.trigger_price = goal
        if limit is not None:
            row.price = limit
            row.qty = target.position.qty_open
            self._order_event(session, row, target, EVENT_GTT_REFRESHED, now)
        session.commit()
        return True

    # --- the 15:25 swap -----------------------------------------------------------------------

    def swap(self, session: Session, position_id: uuid.UUID, now: datetime) -> str:
        """Replace the intraday stop with a GTT for the night (HAT-EXEC-061): the GTT first, the
        intraday stop cancelled only once the GTT is confirmed placed."""
        target = self._target(session, position_id)
        position = target.position
        if position.closed_at is not None or position.qty_open <= 0:
            return CLOSED
        if self.exit_working(session, position.id):
            return EXIT_WORKING  # the sale comes first; the job tries the swap again
        gtts = self.stops_of(session, position.id, *LIVE)
        has_gtt = any(row.purpose == STOP_GTT and row.broker_order_id for row in gtts)
        if not has_gtt:
            unsure = [
                row
                for row in self.stops_of(session, position.id, *UNSURE)
                if row.purpose == STOP_GTT
            ]
            has_gtt = any(self._resend(session, target, row) for row in unsure)
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

    # --- the nightly verify -------------------------------------------------------------------

    def unverified(
        self, session: Session, position_id: uuid.UUID, now: datetime, reason: str
    ) -> str:
        """The verify could not run for the position (its fills unread): `gtt.unverified` is
        alerted and its stops are left as they are."""
        target = self._target(session, position_id)
        self._alert(ALERT_GTT_UNVERIFIED, target, now, reason)
        return FAILED

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
        live = [
            (row, row.broker_order_id) for row in gtts if row.status in LIVE and row.broker_order_id
        ]
        if tick is None:
            # No GTT price can be set without the tick: told, and nothing is ended or placed.
            self._alert(ALERT_GTT_UNVERIFIED, target, now, "no_tick")
            return UNVERIFIED
        if live:
            row, gtt_id = live[-1]
            limit = gtt_limit_price(goal, tick)
            ack = self._broker.modify_gtt(GttModify(gtt_id, position.qty_open, goal, limit))
            if ack.ok:
                row.qty, row.trigger_price, row.price = position.qty_open, goal, limit
                self._order_event(session, row, target, EVENT_GTT_REFRESHED, now)
                session.commit()
                return VERIFIED
            if ack.code == BROKER_UNAVAILABLE or not self._cancel(session, target, row, now):
                # The old GTT may still rest at the broker: a second one could sell the same
                # shares twice. Told, and nothing is placed.
                self._alert(ALERT_GTT_UNVERIFIED, target, now, ack.code)
                return UNVERIFIED
        elif any(row.status == "unconfirmed" for row in gtts):
            # A GTT whose answer was lost has no tag to look it up by and may rest at the
            # broker: told every night until the owner settles it; nothing is placed.
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
