"""Broker fills into HAT's book (07 §4, §6, §11; HAT-EXEC-060, -062, HAT-BRK-111; PR 15c).

`FillSync.run` reads the broker's order book and trade book (both fail closed: an unreadable
book raises and nothing is written) and then, for one broker account:

1. adopts a pending or unconfirmed HAT order the broker shows under its tag
   (`submit.adopt_broker_order`), so a fill never lands on an order HAT thinks was not sent;
2. records each new trade of an HT-tagged order as a fill (`live_trades.apply_entry_fill`,
   `apply_exit_fill`) and has the `StopDesk` protect the position at once: an entry fill gets its
   broker stop before anything else touches the position (HAT-EXEC-060), a sale resizes the stop,
   and the last sale cancels it;
3. attributes the SELL trade of a triggered GTT to that GTT: one under the GTT's own tag
   (`gtt_tag`: the adapter reads a row under one of HAT's GTT tags as that GTT's sale; whether
   Fyers puts the tag on the sale is [verify on a live account]) to the working GTT with that
   tag, and an untagged one to the GTT of the one open position on its symbol; a broker trade id
   is recorded once across all orders, so an old trade never closes a newer position;
4. moves order statuses (`submit.record_broker_status`): an entry that ended unfilled ends its
   signal Cancelled; a day stop that expired leaves its position to be protected again; a sale
   the broker took and then ended short records `order.rejected` and fires its rules again, so the
   next evaluation retries it once; its second rejection raises `exit.not_placed` (HAT-EXEC-075);
5. raises `gtt.unfilled` for such a GTT's SELL order (by its tag, or untagged on its symbol) that
   ended (cancelled, rejected, expired) short of its quantity: the GTT triggered and did not fill
   (07 §6 step 3).
   The GTT is ended and the position protected again. The broker order id is kept on the
   `order.cancelled` event, so the alert is raised once; while the GTT's own sale (at its limit
   price and quantity) still works short of its quantity a minute after it was first seen (the
   price gapped below the limit), it rests: `gtt.triggered` is recorded and `gtt.unfilled`
   raised once, and the sale is asked to go at the market at every sync until the book shows it
   does (HAT-EXEC-066);
6. protects again every open real position of the account that has no stop the broker
   confirmed (a stop the broker refused twice was alerted; a stop whose answer was lost is
   settled by `StopDesk.protect`; each sync tries again);
7. closes each position its sales emptied while its entry still worked, once the broker's book
   shows the entry ended or its trade date passed (`live_trades.close_if_emptied`); while it
   still works, the cancel of its remainder is asked unless it was already (F2). A late fill of
   the remainder lands on the open position in step 2 and is protected like any entry fill.

Each fill and its stop work commit as separate steps (the model's atomic steps, StopPlacement).

`watch_closely` tells the runtime when to read the books every loop step instead of every minute:
while a real entry of today's signals may still fill, or a stop's answer is not in yet, a fill gets
its stop, and an unsure stop is settled, within one step (HAT-EXEC-060).
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..engine import live_trades
from ..engine.trades import TradeError
from ..events import record
from ..models import HatBot, HatEvent, HatFill, HatInstrument, HatOrder, HatPosition, HatSignal
from ..notify import LogNotifier, Notifier
from ..time import trade_date_of
from .broker import BrokerOrder, BrokerTrade, HatBroker, OrderType, Side
from .protect import (
    ALERT_EXIT_NOT_PLACED,
    ALERT_GTT_UNFILLED,
    EVENT_GTT_TRIGGERED,
    EXIT_WORKING,
    LIVE,
    STOP_DAY,
    STOP_GTT,
    UNSURE,
    WORKING,
    StopDesk,
)
from .submit import (
    EXIT_SALES,
    MAX_SALE_REJECTIONS,
    TagBook,
    adopt_broker_order,
    record_broker_status,
    rejected_sales,
)

__all__ = ["ALERT_FILL_EXCESS", "ALERT_FILL_REFUSED", "FillSync", "SyncReport", "watch_closely"]

logger = logging.getLogger(__name__)

ENTRY = "entry"
ALERT_FILL_REFUSED = "fill.refused"
# An entry filled beyond its quantity: its tag reached the broker twice (HAT-BRK-103). Every
# fill is still recorded, so the shares the broker holds are tracked and under the stop.
ALERT_FILL_EXCESS = "fill.excess"
_ENDED_SHORT = ("cancelled", "rejected", "expired")
STOPS = (STOP_DAY, STOP_GTT)
TRIGGER_ORDER = "trigger_order_id"  # the untagged broker order a triggered GTT became
RESTING = "gtt_triggered_resting"  # alert code: the GTT's sale rests below its limit
NOT_MARKETED = "gtt_sale_not_marketed"  # alert code: the broker refused the market conversion
# A GTT's sale working this long is resting, not on its way to a fill (HAT-EXEC-066).
RESTING_AFTER = timedelta(seconds=60)


def watch_closely(session: Session, account_id: uuid.UUID, now: datetime) -> bool:
    """Whether the account's fills are read at every periodic step (HAT-EXEC-060): a real entry of
    its signals of this trade date may still fill (one of an earlier session ended at its close),
    or a stop of an open real position was sent and its answer is not in yet (pending or
    unconfirmed: the next sync settles it). A stop the broker refused is retried once a minute,
    never faster, so a refusal does not spend the tag's sequence (07 §10)."""
    entry = (
        select(HatOrder.id)
        .join(HatSignal, HatSignal.id == HatOrder.signal_id)
        .where(
            HatOrder.broker_account_id == account_id,
            HatOrder.is_virtual.is_(False),
            HatOrder.purpose == ENTRY,
            HatOrder.status.in_(WORKING),
            HatSignal.trade_date == trade_date_of(now),
        )
    )
    stop = (
        select(HatOrder.id)
        .join(HatPosition, HatPosition.id == HatOrder.position_id)
        .where(
            HatOrder.broker_account_id == account_id,
            HatOrder.is_virtual.is_(False),
            HatOrder.purpose.in_(STOPS),
            HatOrder.status.in_(UNSURE),
            HatPosition.closed_at.is_(None),
        )
    )
    return any(session.scalar(query.limit(1)) is not None for query in (entry, stop))


@dataclass(frozen=True)
class _WorkingGtts:
    """The working GTTs of an account's open real positions: by tag (with its symbol), and by
    symbol for each symbol only one of them is on (two GTTs on one symbol cannot be told apart
    by an untagged sale, so neither is attributed one)."""

    by_tag: dict[str, tuple[HatOrder, str]]
    by_symbol: dict[str, HatOrder]

    def of(self, item: BrokerOrder | BrokerTrade) -> HatOrder | None:
        """The GTT a broker sale is the child of: the one under its `gtt_tag` on its symbol;
        for an untagged sale, the one GTT on its symbol; None for anything else (an order HAT
        placed, a foreign one, a GTT tag HAT has no working GTT under)."""
        if item.gtt_tag is not None:
            found = self.by_tag.get(item.gtt_tag)
            return found[0] if found is not None and found[1] == item.symbol else None
        return self.by_symbol.get(item.symbol) if item.tag is None else None


@dataclass
class SyncReport:
    fills: int = 0
    adopted: int = 0
    statuses: int = 0
    unfilled_gtts: int = 0
    resting_gtts: int = 0
    refused: int = 0
    protected: set[uuid.UUID] = field(default_factory=set)


class FillSync:
    """Fills for one broker account's session; `desk` places and moves its stops."""

    def __init__(self, broker: HatBroker, desk: StopDesk, notifier: Notifier | None = None) -> None:
        self._broker = broker
        self._desk = desk
        self._notifier = notifier or LogNotifier()
        self._refused_trades: set[str] = set()
        self._unmarketed: set[str] = set()  # resting GTT sales whose conversion was alerted
        self._working_since: dict[str, datetime] = {}  # a GTT's sale first seen working

    def run(self, session: Session, account_id: uuid.UUID, now: datetime) -> SyncReport:
        """One pass over the broker's books. Raises BrokerUnavailableError when either book
        cannot be read (nothing is written then)."""
        orders = self._broker.orders()
        trades = self._broker.trades()
        report = SyncReport()
        rows = {
            row.tag: row
            for row in session.scalars(
                select(HatOrder).where(
                    HatOrder.broker_account_id == account_id, HatOrder.is_virtual.is_(False)
                )
            )
        }
        book = TagBook.of(orders)
        self._adopt(session, rows, book, now, report)
        for trade in trades:
            row = rows.get(trade.tag) if trade.tag else None
            if row is not None:
                self._apply(session, row, trade, now, report)
        gtts = self._working_gtts(session, account_id)
        for trade in trades:
            gtt = gtts.of(trade)
            if gtt is not None and trade.side is Side.SELL:
                self._apply_gtt_trade(session, gtt, trade, now, report)
        self._statuses(session, rows, book, now, report)
        gtts = self._working_gtts(session, account_id)
        for order in orders:
            gtt = gtts.of(order)
            if gtt is None:
                continue
            if self._unfilled_trigger(order, gtt):
                self._gtt_unfilled(session, gtt, order, now, report)
            elif self._resting_trigger(session, order, gtt):
                self._gtt_resting(session, gtt, order, now, report)
        # a sale that filled or left the book before it counted as resting is forgotten
        working = {order.order_id for order in orders}
        self._working_since = {
            order_id: since
            for order_id, since in self._working_since.items()
            if order_id in working
        }
        for position_id in self._unprotected(session, account_id) - report.protected:
            self._protect(session, position_id, now, report)
        for position_id in self._closed_with_unsure_stops(session, account_id) - report.protected:
            # A sale may close a position while one of its stops is still unsure (HAT-EXEC-065):
            # once settled, a stop that landed is cancelled, so it never rests under no shares.
            self._desk.cancel_all(session, position_id, now)
        for position_id in self._emptied(session, account_id):
            self._close_emptied(session, position_id, now)
        return report

    def _close_emptied(self, session: Session, position_id: uuid.UUID, now: datetime) -> None:
        """Step 7 for one position (one failing leaves the others closed)."""
        try:
            if live_trades.close_if_emptied(session, position_id, now) is None:
                session.rollback()
                self._desk.cancel_entry(session, position_id)  # unless already asked
            else:
                session.commit()
        except Exception:
            session.rollback()
            logger.exception("emptied position %s not closed", position_id)

    # --- 1. adoption ----------------------------------------------------------------------------

    @staticmethod
    def _bot_of(session: Session, row: HatOrder) -> uuid.UUID | None:
        return session.scalar(select(HatSignal.bot_id).where(HatSignal.id == row.signal_id))

    def _adopt(
        self,
        session: Session,
        rows: dict[str, HatOrder],
        book: TagBook,
        now: datetime,
        report: SyncReport,
    ) -> None:
        for row in rows.values():
            found = book.for_row(row) if row.status in UNSURE else None
            if found is not None:
                adopt_broker_order(session, row, found, self._bot_of(session, row))
                session.commit()
                report.adopted += 1
                self._sale_ended(session, row, found, now)

    def _sale_ended(
        self, session: Session, row: HatOrder, found: BrokerOrder, now: datetime
    ) -> None:
        """A sale the broker ended short: once it is the second rejection of the session, the
        sale is not retried this session and `exit.not_placed` is raised (HAT-OPS-032)."""
        if not (
            row.purpose in EXIT_SALES and row.status in _ENDED_SHORT and found.filled_qty < row.qty
        ):
            return
        if rejected_sales(session, row.signal_id, row.purpose, now) < MAX_SALE_REJECTIONS:
            return
        bot_id = self._bot_of(session, row)
        self._notifier.alert(
            ALERT_EXIT_NOT_PLACED,
            trade_date_of(now),
            {
                "bot_id": str(bot_id),
                "signal_id": str(row.signal_id),
                "position_id": str(row.position_id),
                "reason": "order_rejected",
            },
        )

    # --- 2. and 3. fills ------------------------------------------------------------------------

    def _apply(
        self,
        session: Session,
        row: HatOrder,
        trade: BrokerTrade,
        now: datetime,
        report: SyncReport,
    ) -> None:
        position_id = self._record(session, row, trade, now, report)
        if position_id is not None:
            # HAT-EXEC-060: the stop comes before any other action on the position.
            self._protect(session, position_id, now, report)
            if row.purpose == ENTRY:
                self._check_excess(session, row, trade.qty, now)  # the alert after the stop

    def _record(
        self,
        session: Session,
        row: HatOrder,
        trade: BrokerTrade,
        now: datetime,
        report: SyncReport,
    ) -> uuid.UUID | None:
        """Record one broker trade as a fill of `row` (committed); the position it moved, or
        None when the trade was recorded before or refused."""
        fill = live_trades.BrokerFill(
            trade.qty, trade.price, trade.trade_id, trade.traded_at or now
        )
        try:
            if row.purpose == ENTRY:
                position = live_trades.apply_entry_fill(session, row.id, fill)
            else:
                position = live_trades.apply_exit_fill(session, row.id, fill)
        except TradeError as exc:
            session.rollback()
            self._refused(row, trade.trade_id, exc.code, now, report)
            return None
        if position is None:
            session.rollback()  # this broker trade is already recorded
            return None
        session.commit()
        report.fills += 1
        return position.id

    def _check_excess(self, session: Session, row: HatOrder, qty: int, now: datetime) -> None:
        """The fill of `qty` just recorded took the entry's fills above its quantity:
        `fill.excess`, once, on the fill that crosses it (each fill is recorded once; later fills
        of the same excess are not alerted again). The fills stay recorded: shares refused here
        would sit at the broker untracked, with no stop; recorded, the stop covers them and
        reconciliation blocks the account's entries."""
        filled = session.scalar(select(func.sum(HatFill.qty)).where(HatFill.order_id == row.id))
        if not int(filled or 0) - qty <= row.qty < int(filled or 0):
            return
        logger.error("entry %s filled %s of %s", row.tag, filled, row.qty)
        self._notifier.alert(
            ALERT_FILL_EXCESS,
            trade_date_of(now),
            {"tag": row.tag, "qty": row.qty, "filled": int(filled or 0)},
        )

    def _refused(
        self, row: HatOrder, trade_id: str, code: str, now: datetime, report: SyncReport
    ) -> None:
        report.refused += 1
        if trade_id in self._refused_trades:
            return  # told once; the trade stays in the broker's book all day
        self._refused_trades.add(trade_id)
        logger.error("fill for order %s refused (%s)", row.tag, code)
        self._notifier.alert(ALERT_FILL_REFUSED, trade_date_of(now), {"tag": row.tag, "code": code})

    @staticmethod
    def _working_gtts(session: Session, account_id: uuid.UUID) -> _WorkingGtts:
        """The working GTTs of the account's open real positions."""
        found: dict[str, list[HatOrder]] = defaultdict(list)
        rows = session.execute(
            select(HatOrder, HatInstrument.symbol)
            .join(HatPosition, HatPosition.id == HatOrder.position_id)
            .join(HatInstrument, HatInstrument.id == HatPosition.instrument_id)
            .where(
                HatOrder.broker_account_id == account_id,
                HatOrder.purpose == STOP_GTT,
                HatOrder.is_virtual.is_(False),
                HatOrder.status.in_(LIVE),
                HatPosition.closed_at.is_(None),
            )
        ).all()
        for row, symbol in rows:
            found[symbol].append(row)
        return _WorkingGtts(
            {row.tag: (row, symbol) for row, symbol in rows},
            {symbol: items[0] for symbol, items in found.items() if len(items) == 1},
        )

    def _apply_gtt_trade(
        self,
        session: Session,
        gtt: HatOrder,
        trade: BrokerTrade,
        now: datetime,
        report: SyncReport,
    ) -> None:
        seen = session.scalar(select(HatFill.id).where(HatFill.broker_trade_id == trade.trade_id))
        if seen is not None:
            return  # recorded before, on this or an older GTT
        position_id = self._record(session, gtt, trade, now, report)
        if position_id is None:
            return
        filled = sum(session.scalars(select(HatFill.qty).where(HatFill.order_id == gtt.id)).all())
        session.refresh(gtt)
        if gtt.status in LIVE:
            # The GTT triggered: it no longer rests as a stop, so it is marked before the
            # position is protected (a part-filled sale may still sell the rest).
            gtt.status = "filled" if filled >= gtt.qty else "partially_filled"
            session.commit()
        self._protect(session, position_id, now, report)

    def _protect(
        self, session: Session, position_id: uuid.UUID, now: datetime, report: SyncReport
    ) -> None:
        # A position whose sale still works is not counted as protected: once that sale ends
        # (later in this sync, or the next) it is protected again.
        if self._desk.protect(session, position_id, now) != EXIT_WORKING:
            report.protected.add(position_id)

    # --- 4. statuses ----------------------------------------------------------------------------

    def _statuses(
        self,
        session: Session,
        rows: dict[str, HatOrder],
        book: TagBook,
        now: datetime,
        report: SyncReport,
    ) -> None:
        touched: set[uuid.UUID] = set()
        for row in rows.values():
            found = None if row.status in UNSURE else book.for_row(row)
            if found is None:
                continue
            session.refresh(row)
            if record_broker_status(session, row, found, self._bot_of(session, row)):
                session.commit()
                report.statuses += 1
                self._sale_ended(session, row, found, now)
                if row.position_id is not None and row.purpose != ENTRY:
                    touched.add(row.position_id)
        for position_id in touched - report.protected:
            # A stop that ended (a day stop expired at the close) or a sale that ended short:
            # the position must still rest on a broker stop.
            self._desk.protect(session, position_id, now)

    # --- 5. a GTT that triggered without a fill -------------------------------------------------

    @staticmethod
    def _unfilled_trigger(order: BrokerOrder, gtt: HatOrder) -> bool:
        """A GTT's sale (`_WorkingGtts.of` found its GTT) that ended short of its quantity; an
        untagged one only at the GTT's quantity, so another sale on the symbol that ended never
        ends the GTT."""
        return (
            order.side is Side.SELL
            and (order.gtt_tag is not None or order.qty == gtt.qty)
            and order.status in _ENDED_SHORT
            and order.filled_qty < order.qty
        )

    @staticmethod
    def _already_alerted(session: Session, order_id: str, gtt_ids: Iterable[uuid.UUID]) -> bool:
        payloads = session.scalars(
            select(HatEvent.payload).where(
                HatEvent.event_type == "order.cancelled", HatEvent.aggregate_id.in_(list(gtt_ids))
            )
        )
        return any((payload or {}).get(TRIGGER_ORDER) == order_id for payload in payloads)

    def _gtt_unfilled(
        self,
        session: Session,
        gtt: HatOrder,
        order: BrokerOrder,
        now: datetime,
        report: SyncReport,
    ) -> None:
        position = session.get(HatPosition, gtt.position_id)
        signal = session.get(HatSignal, gtt.signal_id)
        if position is None or signal is None:
            return
        gtt_ids = session.scalars(
            select(HatOrder.id).where(
                HatOrder.position_id == position.id, HatOrder.purpose == STOP_GTT
            )
        ).all()
        if self._already_alerted(session, order.order_id, gtt_ids):
            return
        gtt.status = "expired"
        record(
            session,
            aggregate_type="order",
            aggregate_id=gtt.id,
            event_type="order.cancelled",
            actor="broker",
            payload={
                "tag": gtt.tag,
                "purpose": gtt.purpose,
                "broker_order_id": gtt.broker_order_id,
                TRIGGER_ORDER: order.order_id,
                "code": "gtt_triggered_unfilled",
            },
            correlation_id=signal.id,
            bot_id=signal.bot_id,
            occurred_at=now,
        )
        session.commit()
        report.unfilled_gtts += 1
        self._notifier.alert(
            ALERT_GTT_UNFILLED,
            trade_date_of(now),
            {
                "bot_id": str(signal.bot_id),
                "signal_id": str(signal.id),
                "position_id": str(position.id),
                "code": "gtt_triggered_unfilled",
            },
        )
        self._desk.protect(session, position.id, now)

    # --- 5b. a GTT that triggered and whose sale rests unfilled ----------------------------------

    def _resting_trigger(self, session: Session, order: BrokerOrder, gtt: HatOrder) -> bool:
        """The GTT's own sale, still working short of its quantity: a CNC SELL under the GTT's
        tag, or an untagged CNC SELL of the GTT's quantity at the GTT's limit price (once marked,
        by its broker order id: a market sale has no limit price). Any other untagged order is
        not HAT's and is left alone."""
        if (
            order.side is not Side.SELL
            or order.product != "CNC"
            or order.status not in LIVE
            or order.filled_qty >= order.qty
        ):
            return False
        if order.gtt_tag is not None:
            return True  # `_WorkingGtts.of` matched it to this GTT by its tag
        if order.qty != gtt.qty:
            return False
        limit = Decimal(gtt.price) if gtt.price is not None else None
        return (limit is not None and order.price == limit) or self._trigger_seen(
            session, gtt.id, order.order_id
        )

    @staticmethod
    def _trigger_seen(session: Session, gtt_id: uuid.UUID, order_id: str) -> bool:
        payloads = session.scalars(
            select(HatEvent.payload).where(
                HatEvent.aggregate_type == "order",
                HatEvent.aggregate_id == gtt_id,
                HatEvent.event_type == EVENT_GTT_TRIGGERED,
            )
        )
        return any((payload or {}).get(TRIGGER_ORDER) == order_id for payload in payloads)

    def _gtt_resting(
        self,
        session: Session,
        gtt: HatOrder,
        order: BrokerOrder,
        now: datetime,
        report: SyncReport,
    ) -> None:
        """HAT-EXEC-066: the GTT triggered and its limit sale still works (the price gapped below
        the limit). Once the sale has worked `RESTING_AFTER` (a sale filling at once is left to
        fill), `gtt.triggered` is recorded and `gtt.unfilled` raised; from then on every sync
        asks for the sale to go at the market until the book shows it does. The GTT row stays
        live: its sale holds the shares, so no other stop or sale is placed over it."""
        signal = session.get(HatSignal, gtt.signal_id)
        if signal is None:
            return
        alert = {
            "bot_id": str(signal.bot_id),
            "signal_id": str(signal.id),
            "position_id": str(gtt.position_id),
        }
        if not self._trigger_seen(session, gtt.id, order.order_id):
            since = self._working_since.setdefault(order.order_id, now)
            if now - since < RESTING_AFTER:
                return  # it may still fill at its limit; a restart only waits again
            self._working_since.pop(order.order_id, None)
            record(
                session,
                aggregate_type="order",
                aggregate_id=gtt.id,
                event_type=EVENT_GTT_TRIGGERED,
                actor="broker",
                payload={
                    "tag": gtt.tag,
                    "broker_order_id": gtt.broker_order_id,
                    TRIGGER_ORDER: order.order_id,
                    "qty": order.qty,
                    "filled_qty": order.filled_qty,
                },
                correlation_id=signal.id,
                bot_id=signal.bot_id,
                occurred_at=now,
            )
            session.commit()
            report.resting_gtts += 1
            self._notifier.alert(ALERT_GTT_UNFILLED, trade_date_of(now), {**alert, "code": RESTING})
        if order.order_type is OrderType.MARKET:
            return  # already at the market (one at the lower price band waits for a buyer)
        if self._desk.market_sale(order.order_id).ok or order.order_id in self._unmarketed:
            return
        self._unmarketed.add(order.order_id)
        self._notifier.alert(
            ALERT_GTT_UNFILLED, trade_date_of(now), {**alert, "code": NOT_MARKETED}
        )

    # --- 6. positions without a stop ---------------------------------------------------------

    @staticmethod
    def _closed_with_unsure_stops(session: Session, account_id: uuid.UUID) -> set[uuid.UUID]:
        """Closed real positions that still have a stop of unknown outcome (read from the few
        unsure stop orders, not from every closed position)."""
        return set(
            session.scalars(
                select(HatOrder.position_id)
                .join(HatPosition, HatPosition.id == HatOrder.position_id)
                .where(
                    HatOrder.broker_account_id == account_id,
                    HatOrder.is_virtual.is_(False),
                    HatOrder.purpose.in_(STOPS),
                    HatOrder.status.in_(UNSURE),
                    or_(HatPosition.closed_at.is_not(None), HatPosition.qty_open <= 0),
                )
                .distinct()
            )
        )

    @staticmethod
    def _emptied(session: Session, account_id: uuid.UUID) -> list[uuid.UUID]:
        """Open real positions of the account that hold no shares (sold while the entry worked)."""
        return list(
            session.scalars(
                select(HatPosition.id)
                .join(HatBot, HatBot.id == HatPosition.bot_id)
                .where(
                    HatBot.broker_account_id == account_id,
                    HatPosition.is_virtual.is_(False),
                    HatPosition.closed_at.is_(None),
                    HatPosition.qty_open <= 0,
                )
            )
        )

    @staticmethod
    def _unprotected(session: Session, account_id: uuid.UUID) -> set[uuid.UUID]:
        # Only a stop the broker confirmed protects: one reserved and not sent, or sent with its
        # answer lost, is `protect`'s to send or settle (HAT-EXEC-065).
        working = (
            select(HatOrder.id)
            .where(
                HatOrder.position_id == HatPosition.id,
                HatOrder.purpose.in_(STOPS),
                HatOrder.status.in_(LIVE),
            )
            .exists()
        )
        # A live stop for another quantity than the position holds (a refused resize): protect
        # resizes it again.
        short = (
            select(HatOrder.id)
            .where(
                HatOrder.position_id == HatPosition.id,
                HatOrder.purpose.in_(STOPS),
                HatOrder.status == "placed",
                HatOrder.qty != HatPosition.qty_open,
            )
            .exists()
        )
        return set(
            session.scalars(
                select(HatPosition.id)
                .join(HatBot, HatBot.id == HatPosition.bot_id)
                .where(
                    HatBot.broker_account_id == account_id,
                    HatPosition.is_virtual.is_(False),
                    HatPosition.closed_at.is_(None),
                    HatPosition.qty_open > 0,
                    ~working | short,
                )
            )
        )
