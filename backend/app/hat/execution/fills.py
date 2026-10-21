"""Broker fills into HAT's book (07 §4, §6, §11; HAT-EXEC-060, -062, HAT-BRK-111; PR 15c).

`FillSync.run` reads the broker's order book and trade book (both fail closed: an unreadable
book raises and nothing is written) and then, for one broker account:

1. adopts a pending or unconfirmed HAT order the broker shows under its tag
   (`submit.adopt_broker_order`), so a fill never lands on an order HAT thinks was not sent;
2. records each new trade of an HT-tagged order as a fill (`live_trades.apply_entry_fill`,
   `apply_exit_fill`) and has the `StopDesk` protect the position at once: an entry fill gets its
   broker stop before anything else touches the position (HAT-EXEC-060), a sale resizes the stop,
   and the last sale cancels it;
3. attributes an untagged SELL trade on the symbol of the one position with a working GTT to that
   GTT, since Fyers GTTs carry no tag [verify on a live account]; a broker trade id is recorded
   once across all orders, so an old trade never closes a newer position;
4. moves order statuses (`submit.record_broker_status`): an entry that ended unfilled ends its
   signal Cancelled; a day stop that expired leaves its position to be protected again;
5. raises `gtt.unfilled` for an untagged SELL order on such a symbol that ended (cancelled,
   rejected, expired) short of its quantity: the GTT triggered and did not fill (07 §6 step 3).
   The GTT is ended and the position protected again. The broker order id is kept on the
   `order.cancelled` event, so the alert is raised once;
6. protects again every open real position of the account that has no working stop (a stop
   the broker refused twice was alerted; each sync tries it again).

Each fill and its stop work commit as separate steps (the model's atomic steps, StopPlacement).
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..engine import live_trades
from ..engine.trades import TradeError
from ..events import record
from ..models import HatBot, HatEvent, HatFill, HatInstrument, HatOrder, HatPosition, HatSignal
from ..notify import LogNotifier, Notifier
from ..time import trade_date_of
from .broker import BrokerOrder, BrokerTrade, HatBroker, Side
from .protect import ALERT_GTT_UNFILLED, LIVE, STOP_DAY, STOP_GTT, UNSURE, WORKING, StopDesk
from .submit import adopt_broker_order, record_broker_status
from .tags import is_hat_tag

__all__ = ["ALERT_FILL_REFUSED", "FillSync", "SyncReport"]

logger = logging.getLogger(__name__)

ENTRY = "entry"
ALERT_FILL_REFUSED = "fill.refused"
_ENDED_SHORT = ("cancelled", "rejected", "expired")
STOPS = (STOP_DAY, STOP_GTT)
TRIGGER_ORDER = "trigger_order_id"  # the untagged broker order a triggered GTT became


@dataclass
class SyncReport:
    fills: int = 0
    adopted: int = 0
    statuses: int = 0
    unfilled_gtts: int = 0
    refused: int = 0
    protected: set[uuid.UUID] = field(default_factory=set)


class FillSync:
    """Fills for one broker account's session; `desk` places and moves its stops."""

    def __init__(self, broker: HatBroker, desk: StopDesk, notifier: Notifier | None = None) -> None:
        self._broker = broker
        self._desk = desk
        self._notifier = notifier or LogNotifier()
        self._refused_trades: set[str] = set()

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
        by_tag = {o.tag: o for o in orders if is_hat_tag(o.tag)}
        self._adopt(session, rows, by_tag, report)
        for trade in trades:
            row = rows.get(trade.tag) if trade.tag else None
            if row is not None:
                self._apply(session, row, trade, now, report)
        gtts = self._working_gtts(session, account_id)
        for trade in trades:
            if trade.tag is None and trade.side is Side.SELL and trade.symbol in gtts:
                self._apply_gtt_trade(session, gtts[trade.symbol], trade, now, report)
        self._statuses(session, rows, by_tag, now, report)
        gtts = self._working_gtts(session, account_id)
        for order in orders:
            if self._unfilled_trigger(order, gtts):
                self._gtt_unfilled(session, gtts[order.symbol], order, now, report)
        for position_id in self._unprotected(session, account_id) - report.protected:
            self._desk.protect(session, position_id, now)
            report.protected.add(position_id)
        return report

    # --- 1. adoption ----------------------------------------------------------------------------

    @staticmethod
    def _bot_of(session: Session, row: HatOrder) -> uuid.UUID | None:
        return session.scalar(select(HatSignal.bot_id).where(HatSignal.id == row.signal_id))

    def _adopt(
        self,
        session: Session,
        rows: dict[str, HatOrder],
        by_tag: dict[str | None, BrokerOrder],
        report: SyncReport,
    ) -> None:
        for tag, row in rows.items():
            if row.status in UNSURE and tag in by_tag:
                adopt_broker_order(session, row, by_tag[tag], self._bot_of(session, row))
                session.commit()
                report.adopted += 1

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
            self._desk.protect(session, position_id, now)
            report.protected.add(position_id)

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
    def _working_gtts(session: Session, account_id: uuid.UUID) -> dict[str, HatOrder]:
        """Symbol -> the working GTT of the one open real position on it (a symbol two open
        positions share cannot be told apart, so neither is attributed)."""
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
        return {symbol: items[0] for symbol, items in found.items() if len(items) == 1}

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
        self._desk.protect(session, position_id, now)
        report.protected.add(position_id)

    # --- 4. statuses ----------------------------------------------------------------------------

    def _statuses(
        self,
        session: Session,
        rows: dict[str, HatOrder],
        by_tag: dict[str | None, BrokerOrder],
        now: datetime,
        report: SyncReport,
    ) -> None:
        touched: set[uuid.UUID] = set()
        for tag, found in by_tag.items():
            row = rows.get(tag) if tag else None
            if row is None or row.status in UNSURE:
                continue
            session.refresh(row)
            if record_broker_status(session, row, found, self._bot_of(session, row)):
                session.commit()
                report.statuses += 1
                if row.position_id is not None and row.purpose != ENTRY:
                    touched.add(row.position_id)
        for position_id in touched - report.protected:
            # A stop that ended (a day stop expired at the close) or a sale that ended short:
            # the position must still rest on a broker stop.
            self._desk.protect(session, position_id, now)

    # --- 5. a GTT that triggered without a fill -------------------------------------------------

    @staticmethod
    def _unfilled_trigger(order: BrokerOrder, gtts: dict[str, HatOrder]) -> bool:
        return (
            order.tag is None
            and order.side is Side.SELL
            and order.symbol in gtts
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

    # --- 6. positions without a stop ---------------------------------------------------------

    @staticmethod
    def _unprotected(session: Session, account_id: uuid.UUID) -> set[uuid.UUID]:
        working = (
            select(HatOrder.id)
            .where(
                HatOrder.position_id == HatPosition.id,
                HatOrder.purpose.in_(STOPS),
                HatOrder.status.in_(WORKING),
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
