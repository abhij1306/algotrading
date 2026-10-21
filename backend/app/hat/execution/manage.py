"""Live trade management of real positions (07 §6, §7; HAT-EXEC-060..064, -073, -074,
HAT-OPS-023, -024, -032, -034; HAT-DEC-B17, B18; PR 15c).

`TradeManager` serves one broker account's session, beside the `ExecutionRouter` that places its
entries. It runs the exit stack on real positions and the jobs around the close:

- **Live price events** (`on_price`): the same `exits.evaluate` as shadow positions (T12) via
  `live_trades.evaluate_real_position`; a raised stop is followed at the broker
  (`StopDesk.follow`), and a sale is sent at once as a MARKET SELL CNC. Exits never wait for the
  user and never check a kill switch or pause (06 §6). `hard_stop` is the broker's: while a stop
  rests at the broker the engine sends no sale of its own for it (two sales would oversell); a
  position without a working broker stop is sold by the engine.
- **`exits.close`** (`close_exits`, 15:20-15:25 IST, HAT-EXEC-074): every open position of the
  account's bots, virtual and real, is evaluated on the live price in the close phase, and each
  `close_below_sma` evaluation is recorded with its live price (`exit.close_evaluated`,
  HAT-OPS-034). The body refuses to run outside the window and sends no sale after 15:30.
- **`gtt.swap`** (`swap_stops`, ~15:25) and **`gtt.verify`** (`verify_stops`, nightly): through
  the `StopDesk`; the verify first reads the broker's fills so a position the GTT sold is closed.
- **Session end** (`end_session`): an entry whose order the broker's book does not hold after a
  successful read (its outcome was unknown) ends its signal Rejected with `entry_not_found`; a
  DAY limit cannot fill after the close. (An entry first found already ended is settled when it
  is adopted: `submit.adopt_broker_order`.)

A sale is retried once after a rejection with the next tag, and at most two rejected sales of one
purpose are sent per signal (HAT-OPS-032); then `exit.not_placed` is raised and the broker stop
keeps protecting the position. Nothing is sent before the session's reconciliation.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from time import monotonic

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..engine import exits, live_trades, trades
from ..models import HatBot, HatInstrument, HatOrder, HatPosition, HatSignal
from ..notify import LogNotifier, Notifier
from ..risk.sessions import SessionCalendar, SessionState
from ..time import IST, trade_date_of
from .broker import BrokerUnavailableError, HatBroker, OrderType, Side
from .fills import FillSync, SyncReport
from .guard import ExecutionGuard
from .protect import CLOSED, FAILED, REPLACED, SWAPPED, UNSURE, VERIFIED, StopDesk
from .reconcile import ReconciliationService
from .submit import (
    OrderRefusedError,
    OrderRequest,
    SendOutcome,
    UnresolvedOrderError,
    reserve_order,
    send_order,
)

__all__ = [
    "ALERT_EXIT_NOT_PLACED",
    "ALERT_SALE_REJECTED",
    "EXITS_CLOSE_FROM",
    "EXITS_CLOSE_UNTIL",
    "ORDERS_UNTIL",
    "FLATTEN",
    "CloseReport",
    "FlattenReport",
    "StopsReport",
    "TradeManager",
    "ViewSource",
    "in_close_window",
]

logger = logging.getLogger(__name__)

ENTRY = "entry"
EXIT_FULL = "exit_full"
EXIT_PARTIAL = "exit_partial"
# HAT-EXEC-074, HAT-DEC-B18: the close-phase rules run between 15:20 and 15:25 IST on the live
# price, and their sales go out before the 15:30 close.
EXITS_CLOSE_FROM = time(15, 20)
EXITS_CLOSE_UNTIL = time(15, 25)
ORDERS_UNTIL = time(15, 30)
MAX_SALE_REJECTIONS = 2  # the original and one retry (HAT-OPS-032)
ALERT_SALE_REJECTED = "order.rejected"
ALERT_EXIT_NOT_PLACED = "exit.not_placed"
ENTRY_NOT_FOUND = "entry_not_found"
FLATTEN = "flatten"

# The live view of a position at the close (price, SMA levels, days held); None when unknown.
ViewSource = Callable[[Session, HatPosition], exits.ExitView | None]


def in_close_window(now: datetime) -> bool:
    """15:20 <= now < 15:25 IST."""
    local = now.astimezone(IST).time()
    return EXITS_CLOSE_FROM <= local < EXITS_CLOSE_UNTIL


@dataclass
class CloseReport:
    evaluated: int = 0
    sales: int = 0
    recorded: int = 0
    failed: int = 0
    refused: str | None = None


@dataclass
class FlattenReport:
    """What a flatten did: the open real positions it found, the sales it sent and the
    positions whose sale failed with an error (each logged; the others are still sold)."""

    positions: int = 0
    sent: int = 0
    failed: int = 0


@dataclass
class StopsReport:
    done: int = 0
    failed: list[uuid.UUID] = field(default_factory=list)


class TradeManager:
    """Exits and broker stops of one broker account's real positions (and the account's virtual
    ones at the close)."""

    def __init__(
        self,
        broker: HatBroker,
        guard: ExecutionGuard,
        reconciler: ReconciliationService,
        desk: StopDesk,
        fills: FillSync,
        *,
        calendar: SessionCalendar | None,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] | None = None,
        ticker: Callable[[], float] = monotonic,
    ) -> None:
        self._broker = broker
        self._guard = guard
        self._reconciler = reconciler
        self._desk = desk
        self._fills = fills
        self._calendar = calendar
        self._notifier = notifier or LogNotifier()
        # The time a sale is sent at: `clock` when given, else the caller's `now` plus the time
        # the run has taken so far (`ticker`, monotonic seconds).
        self._clock = clock
        self._ticker = ticker
        # One sale of a position at a time across threads: the live tick path, the close job
        # and the Telegram flatten all sell through `_sell_qty`, whose "nothing else is selling"
        # check and reservation must not interleave (each sale holds its position's lock from
        # that check until the order is sent).
        self._sale_locks: dict[uuid.UUID, threading.Lock] = {}
        self._sale_locks_guard = threading.Lock()

    def _sale_lock(self, position_id: uuid.UUID) -> threading.Lock:
        with self._sale_locks_guard:
            return self._sale_locks.setdefault(position_id, threading.Lock())

    # --- reading ------------------------------------------------------------------------------

    @staticmethod
    def _bots(session: Session, account_id: uuid.UUID) -> list[uuid.UUID]:
        return list(
            session.scalars(select(HatBot.id).where(HatBot.broker_account_id == account_id))
        )

    def open_positions(
        self, session: Session, account_id: uuid.UUID, *, real_only: bool = True
    ) -> list[HatPosition]:
        """The account's open positions (real only, or with the virtual ones), oldest first."""
        query = select(HatPosition).where(
            HatPosition.bot_id.in_(self._bots(session, account_id)),
            HatPosition.closed_at.is_(None),
            HatPosition.qty_open > 0,
        )
        if real_only:
            query = query.where(HatPosition.is_virtual.is_(False))
        return list(session.scalars(query.order_by(HatPosition.opened_at, HatPosition.id)))

    def orders_allowed(self, now: datetime) -> bool:
        """A sale goes out only inside the calendar session and before 15:30 IST."""
        return self._orders_allowed(now)

    def _orders_allowed(self, now: datetime) -> bool:
        open_now = self._calendar is not None and self._calendar.state(now) is SessionState.OPEN
        return open_now and now.astimezone(IST).time() < ORDERS_UNTIL

    def _reconciled(self, session: Session, account_id: uuid.UUID) -> bool:
        """HAT-BRK-110: nothing is sent before this session's reconciliation."""
        if self._guard.reconciled:
            return True
        try:
            self._reconciler.run(session, account_id)
        except BrokerUnavailableError:
            return False
        return self._guard.reconciled

    # --- live price events --------------------------------------------------------------------

    def on_price(
        self, session: Session, position_id: uuid.UUID, view: exits.ExitView, now: datetime
    ) -> exits.Evaluation:
        """Run the live rules of one real position on a price event and carry them out."""
        return self._evaluate(session, position_id, view, "live", now, self._sending_clock(now))

    def _sending_clock(self, now: datetime) -> Callable[[], datetime]:
        """The clock a run reads at each sale: a close run that started at 15:24 and is still
        sending at 15:30 sends nothing more (HAT-EXEC-074)."""
        if self._clock is not None:
            return self._clock
        start = self._ticker()
        return lambda: now + timedelta(seconds=self._ticker() - start)

    def _evaluate(
        self,
        session: Session,
        position_id: uuid.UUID,
        view: exits.ExitView,
        phase: exits.Phase,
        now: datetime,
        sending: Callable[[], datetime],
    ) -> exits.Evaluation:
        result = live_trades.evaluate_real_position(session, position_id, view, phase, now)
        session.commit()
        if result.new_stop is not None:
            self._desk.follow(session, position_id, now)
        for action in result.actions:
            self._sell(session, position_id, action, now, sending)
        return result

    # --- flatten (HAT-TG-035, HAT-GOV-082) ----------------------------------------------------

    def flatten(self, session: Session, account_id: uuid.UUID, now: datetime) -> FlattenReport:
        """Sell every open real position of the account at market (`purpose = flatten`, side
        sell, the position's whole `qty_open`), through the exit path: the same session,
        reconciliation and stop checks as any exit, never a kill or pause check. It can only
        reduce exposure. A position already being sold is left to that sale. Virtual positions
        are measurements, not exposure, and are left alone. The confirmation and the
        `flatten.executed` event are the caller's (the Telegram edge)."""
        report = FlattenReport()
        sending = self._sending_clock(now)
        for position in self.open_positions(session, account_id):
            report.positions += 1
            try:
                sent = self._sell_qty(
                    session, position, FLATTEN, position.qty_open, FLATTEN, (), now, sending
                )
            except Exception:  # one position's failure must not stop the others being sold
                session.rollback()
                logger.exception("flatten of position %s failed", position.id)
                report.failed += 1
                continue
            if sent is not None and sent.status != "rejected":
                report.sent += 1
        return report

    # --- sales --------------------------------------------------------------------------------

    def _sell(
        self,
        session: Session,
        position_id: uuid.UUID,
        action: exits.ExitAction,
        now: datetime,
        sending: Callable[[], datetime],
    ) -> SendOutcome | None:
        position = session.get(HatPosition, position_id)
        if position is None:
            return None
        session.refresh(position)
        rule = action.rule_ids[0].split(":", 1)[1]
        if rule == live_trades.HARD_STOP and self._desk.stops_of(session, position.id):
            # The stop resting at the broker is the hard stop (one whose answer was lost may
            # rest there too); HAT sells at market only when no stop may be working.
            return None
        purpose = EXIT_FULL if action.kind == "full" else EXIT_PARTIAL
        qty = position.qty_open if action.kind == "full" else min(action.qty, position.qty_open)
        return self._sell_qty(session, position, purpose, qty, rule, action.rule_ids, now, sending)

    def _sell_qty(
        self,
        session: Session,
        position: HatPosition,
        purpose: str,
        qty: int,
        rule: str,
        rule_ids: Sequence[str],
        now: datetime,
        sending: Callable[[], datetime],
    ) -> SendOutcome | None:
        """Sell `qty` of a real position at market under `purpose` (an exit or a flatten), when
        nothing else is selling it, the session allows it and the stop made room. Every refusal
        is alerted; `rule_ids` are marked fired once the sale is out. Sales of one position are
        serialised (`_sale_lock`); the position is re-read under the lock and `qty` capped at
        what is still open."""
        with self._sale_lock(position.id):
            session.refresh(position)
            qty = min(qty, position.qty_open)
            if qty <= 0:
                return None
            return self._sell_locked(session, position, purpose, qty, rule, rule_ids, now, sending)

    def _sell_locked(
        self,
        session: Session,
        position: HatPosition,
        purpose: str,
        qty: int,
        rule: str,
        rule_ids: Sequence[str],
        now: datetime,
        sending: Callable[[], datetime],
    ) -> SendOutcome | None:
        if position.closed_at is not None or self._desk.exit_working(session, position.id):
            return None  # one sale at a time; the next evaluation sees what it left
        signal = session.get(HatSignal, position.signal_id)
        bot = session.get(HatBot, position.bot_id)
        symbol = session.scalar(
            select(HatInstrument.symbol).where(HatInstrument.id == position.instrument_id)
        )
        if signal is None or bot is None or symbol is None:
            return None
        # Checked at the moment of sending: a close job that started at 15:24 still sends
        # nothing from 15:30 on (HAT-EXEC-074).
        if not self._orders_allowed(sending()):
            self._not_placed(signal, position, "market_closed", now)
            return None
        if not self._reconciled(session, bot.broker_account_id):
            self._not_placed(signal, position, "not_reconciled", now)
            return None
        if self._rejections(session, signal.id, purpose, now) >= MAX_SALE_REJECTIONS:
            return None  # refused twice today and alerted then; tried again the next session
        if not self._desk.make_room(session, position.id, position.qty_open - qty, now):
            self._not_placed(signal, position, "stop_not_moved", now)
            return None
        request = OrderRequest(
            bot_id=signal.bot_id,
            signal_id=signal.id,
            broker_account_id=bot.broker_account_id,
            purpose=purpose,
            side=Side.SELL,
            qty=qty,
            order_type=OrderType.MARKET,
            symbol=symbol,
            position_id=position.id,
        )
        return self._send_sale(session, signal, position, request, rule_ids, rule, now)

    @staticmethod
    def _rejections(session: Session, signal_id: uuid.UUID, purpose: str, now: datetime) -> int:
        """Sales of `purpose` the broker rejected today for the signal."""
        start, end = trades._day_bounds(trade_date_of(now))
        count = session.scalar(
            select(func.count())
            .select_from(HatOrder)
            .where(
                HatOrder.signal_id == signal_id,
                HatOrder.purpose == purpose,
                HatOrder.status == "rejected",
                HatOrder.placed_at >= start,
                HatOrder.placed_at < end,
            )
        )
        return int(count or 0)

    def _send_sale(
        self,
        session: Session,
        signal: HatSignal,
        position: HatPosition,
        request: OrderRequest,
        rule_ids: Sequence[str],
        rule: str,
        now: datetime,
    ) -> SendOutcome | None:
        sent: SendOutcome | None = None
        left = MAX_SALE_REJECTIONS - self._rejections(session, signal.id, request.purpose, now)
        for _ in range(left):
            try:
                tag = reserve_order(session, request)
                sent = send_order(
                    session,
                    self._broker,
                    self._guard,
                    tag,
                    symbol=request.symbol,
                    bot_id=signal.bot_id,
                    rule=rule,
                )
            except (OrderRefusedError, BrokerUnavailableError) as exc:
                session.rollback()
                code = getattr(exc, "code", "broker_unavailable")
                self._release(session, signal.id, request.purpose)
                self._not_placed(signal, position, code, now)
                self._desk.protect(session, position.id, now)  # the stop made room for it
                return sent
            except UnresolvedOrderError:
                return sent
            if sent.status != "rejected":
                # Placed, or unknown (looked up by tag before any retry): the rule has fired.
                if rule_ids:
                    live_trades.mark_fired(session, position.id, rule_ids)
                session.commit()
                return sent
            self._notifier.alert(
                ALERT_SALE_REJECTED,
                trade_date_of(now),
                {"signal_id": str(signal.id), "tag": tag, "code": sent.code},
            )
        self._not_placed(signal, position, "order_rejected", now)
        self._desk.protect(session, position.id, now)
        return sent

    @staticmethod
    def _release(session: Session, signal_id: uuid.UUID, purpose: str) -> None:
        """Close a reserved sale that provably never reached the broker as cancelled."""
        for row in session.scalars(
            select(HatOrder).where(
                HatOrder.signal_id == signal_id,
                HatOrder.purpose == purpose,
                HatOrder.status == "pending",
                HatOrder.placed_at.is_(None),
            )
        ):
            row.status = "cancelled"
        session.commit()

    def _not_placed(
        self, signal: HatSignal, position: HatPosition, reason: str, now: datetime
    ) -> None:
        self._notifier.alert(
            ALERT_EXIT_NOT_PLACED,
            trade_date_of(now),
            {
                "bot_id": str(signal.bot_id),
                "signal_id": str(signal.id),
                "position_id": str(position.id),
                "reason": reason,
            },
        )

    # --- exits.close (15:20-15:25) ------------------------------------------------------------

    def close_exits(
        self, session: Session, account_id: uuid.UUID, views: ViewSource, now: datetime
    ) -> CloseReport:
        """The `exits.close` job body: the close-phase rules of every open position of the
        account's bots on the live price (HAT-EXEC-074), each `close_below_sma` evaluation
        recorded with that price (HAT-OPS-034). Refused outside 15:20-15:25 IST or the session."""
        report = CloseReport()
        if not in_close_window(now) or not self._orders_allowed(now):
            report.refused = "outside_close_window"
            return report
        sending = self._sending_clock(now)
        for position in self.open_positions(session, account_id, real_only=False):
            try:
                self._close_one(session, position, views, now, sending, report)
            except Exception:
                # One position's failure leaves the others evaluated; the job fails and its
                # retry runs again (fired rules and the evaluation record are not repeated).
                session.rollback()
                logger.exception("close-phase exits of position %s failed", position.id)
                report.failed += 1
        return report

    def _close_one(
        self,
        session: Session,
        position: HatPosition,
        views: ViewSource,
        now: datetime,
        sending: Callable[[], datetime],
        report: CloseReport,
    ) -> None:
        view = views(session, position)
        if view is None or view.price is None:
            return  # no live price: nothing fires (missing data never fires a rule)
        if position.is_virtual:
            result = trades.evaluate_position(session, position.id, view, "close", now)
            session.commit()
        else:
            result = self._evaluate(session, position.id, view, "close", now, sending)
            report.sales += len(result.actions)
        report.evaluated += 1
        day = trade_date_of(now)
        if live_trades.record_close_evaluation(session, position.id, view, result, day, now):
            report.recorded += 1
        session.commit()

    # --- gtt.swap (~15:25) and gtt.verify (nightly) -------------------------------------------

    def swap_stops(self, session: Session, account_id: uuid.UUID, now: datetime) -> StopsReport:
        """The `gtt.swap` job body: every open real position's intraday stop becomes a GTT."""
        report = StopsReport()
        if not self._reconciled(session, account_id):
            report.failed = [p.id for p in self.open_positions(session, account_id)]
            return report
        for position in self.open_positions(session, account_id):
            outcome = self._desk.swap(session, position.id, now)
            if outcome == SWAPPED:
                report.done += 1
            elif outcome != CLOSED:
                report.failed.append(position.id)
        return report

    def sync(self, session: Session, account_id: uuid.UUID, now: datetime) -> SyncReport | None:
        """Read the broker's fills into HAT's book (after this session's reconciliation)."""
        if not self._reconciled(session, account_id):
            return None
        try:
            return self._fills.run(session, account_id, now)
        except BrokerUnavailableError:
            return None

    def verify_stops(self, session: Session, account_id: uuid.UUID, now: datetime) -> StopsReport:
        """The `gtt.verify` job body (HAT-EXEC-063, HAT-OPS-024): the broker's fills first, then
        every open real position's GTT refreshed, or alerted and placed again."""
        report = StopsReport()
        # Fills unread (unreconciled, or the book unreadable): the open quantities may be stale,
        # so no GTT is modified or placed; each position is alerted `gtt.unverified`.
        fresh = self.sync(session, account_id, now) is not None
        for position in self.open_positions(session, account_id):
            outcome = (
                self._desk.verify(session, position.id, now)
                if fresh
                else self._desk.unverified(session, position.id, now, "fills_unread")
            )
            if outcome == FAILED:
                report.failed.append(position.id)
            elif outcome in (VERIFIED, REPLACED):
                report.done += 1
        return report

    # --- the session end ----------------------------------------------------------------------

    def end_session(self, session: Session, account_id: uuid.UUID, now: datetime) -> int:
        """After the close: an entry whose order may have been sent but that the broker's book
        does not hold ends its signal Rejected (`entry_not_found`); a reserved entry that was
        never sent is cancelled with it. Returns the number of signals ended. Nothing is ended
        when the book cannot be read."""
        rows = list(
            session.scalars(
                select(HatOrder).where(
                    HatOrder.broker_account_id == account_id,
                    HatOrder.purpose == ENTRY,
                    HatOrder.is_virtual.is_(False),
                    HatOrder.status.in_(UNSURE),
                )
            )
        )
        if not rows:
            return 0
        try:
            book = {o.tag for o in self._broker.orders()}
        except BrokerUnavailableError:
            return 0
        ended = 0
        for row in rows:
            if row.tag in book:
                continue  # the next sync adopts it
            row.status = "cancelled" if row.placed_at is None else "expired"
            if trades.reject_signal(session, row.signal_id, ENTRY_NOT_FOUND, now):
                ended += 1
            session.commit()
        return ended
