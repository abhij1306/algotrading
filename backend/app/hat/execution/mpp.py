"""Market price protection (PR 48, behind `HAT_MPP_RESEND`, off by default).

The broker turns a market sale into a limit a band from the price, which rests unfilled once the
price falls through it. `MarketProtection` holds the whole re-send, so it can be enabled or
deleted as a unit; `submit` keeps only its hooks (`mark_resend`, and `rejected_sales`, which
does not count a re-send as a rejection), the fill sync and the `TradeManager` call in here.

- **Reading the book** (`read_book`, the fill sync's market phase): each sale HAT cancelled to
  send again that the book now shows ended is reported (`SyncReport.resend`); the price the
  book shows for each of HAT's working sales is recorded on its row (a market sale shows its
  protected limit), and each market sale still working short of its quantity `RESTING_AFTER`
  after its send is reported (`SyncReport.resting`).
- **Sending again** (`after_sync`, with `enabled`): a resting market sale is cancelled
  (`order.cancelled`, code `sale_resend`: not a rejection) and, once the book shows it ended, the
  shares it left are sent again as a fresh market sale through the exit path, at every sync
  until it fills. A re-send that could not follow before the order cutoff leaves it resting.
- **A triggered GTT's market sale** (`remarket`, with `enabled`): one the book shows at the
  market but still resting is asked to go at the market again at each sync.
- **Alerts**: while the instrument's last trade is at its lower circuit the sale is left in the
  queue (a re-send would lose its place) and alerted once (`sale.unfilled`, code
  `lower_circuit_locked`); the owner is paged URGENT at the `page_after`th miss of a sale
  (`sale.unfilled`, code `sale_resting`).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import HatFill, HatOrder, HatPosition, HatSignal
from ..notify import Notifier
from ..time import as_utc, day_bounds, trade_date_of
from .broker import BrokerOrder, HatBroker, OrderType
from .fills import ALERT_SALE_UNFILLED, RESTING_AFTER, SaleBand, SyncReport
from .manage import EXIT_PARTIAL
from .order_book import EXIT_SALES, TagBook, mark_resend, resend_event, resend_requested
from .stop_plans import LIVE

if TYPE_CHECKING:
    from .protect import StopDesk

__all__ = [
    "LOWER_CIRCUIT_LOCKED",
    "SALE_RESTING",
    "MarketProtection",
    "SaleSender",
    "resends",
]

logger = logging.getLogger(__name__)

# A market sale that keeps resting unfilled (market price protection made it a limit the price
# left behind) pages the owner (`sale.unfilled`); one at the lower circuit cannot be sold.
SALE_RESTING = "sale_resting"  # alert code: the sale missed `page_after` times
LOWER_CIRCUIT_LOCKED = "lower_circuit_locked"  # alert code: no re-send while locked


class SaleSender(Protocol):
    """The exit path a re-send goes through (`TradeManager`)."""

    def orders_allowed(self, now: datetime) -> bool:
        """A sale can still go out at `now`."""
        ...

    def sell_again(
        self, session: Session, position: HatPosition, row: HatOrder, qty: int, now: datetime
    ) -> None:
        """A fresh market sale of `qty` shares under the purpose and rules of the sale `row`."""
        ...


def resends(session: Session, signal_id: uuid.UUID, purpose: str, now: datetime) -> int:
    """Sales of `purpose` for the signal sent today that HAT cancelled to send again fresh: each
    is one miss of a market sale that rested unfilled."""
    start, end = day_bounds(trade_date_of(now))
    count = session.scalar(
        select(func.count())
        .select_from(HatOrder)
        .where(
            HatOrder.signal_id == signal_id,
            HatOrder.purpose == purpose,
            HatOrder.placed_at >= start,
            HatOrder.placed_at < end,
            resend_event(),
        )
    )
    return int(count or 0)


def _record_book_price(session: Session, row: HatOrder, found: BrokerOrder) -> None:
    """Record on the sale's row the price the book shows for it (a market sale under market
    price protection shows its protected limit)."""
    if found.price is None or (row.price is not None and Decimal(row.price) == found.price):
        return
    row.price = found.price
    session.commit()
    logger.info(
        "sale %s shows %s at %s in the book",
        row.tag,
        found.order_type.value if found.order_type else "unknown",
        found.price,
    )


class MarketProtection:
    """Market price protection for one broker account's session, shared by its `FillSync` and
    `TradeManager`. `enabled` is `HAT_MPP_RESEND`; `band` the day's lower band."""

    def __init__(
        self,
        broker: HatBroker,
        desk: StopDesk,
        notifier: Notifier,
        *,
        band: SaleBand | None = None,
        enabled: bool = False,
        page_after: int = 3,
    ) -> None:
        self._broker = broker
        self._desk = desk
        self._notifier = notifier
        self._band = band
        self.enabled = enabled
        self._page_after = page_after
        self._gtt_misses: dict[str, int] = {}  # a triggered GTT's market sale asked again
        self._locked_alerted: set[str] = set()  # sales alerted at the lower circuit

    # --- the fill sync ----------------------------------------------------------------------------

    @staticmethod
    def read_book(
        session: Session,
        rows: Iterable[HatOrder],
        ended: Iterable[HatOrder],
        book: TagBook,
        now: datetime,
        report: SyncReport,
    ) -> None:
        """The fill sync's market phase over HAT's sales: each of the rows whose status this sync
        moved (`ended`) that HAT cancelled to send again is due its re-send; then the price the
        book shows for each working sale is recorded, and each market sale still working short
        of its quantity `RESTING_AFTER` after it was sent is reported."""
        for row in ended:
            if (
                row.purpose in EXIT_SALES
                and row.status == "cancelled"
                and resend_requested(session, row)
            ):
                report.resend.append(row.id)
        for row in rows:
            if row.purpose not in EXIT_SALES or row.status not in LIVE:
                continue
            found = book.for_row(row)
            if found is None or found.status not in LIVE:
                continue
            _record_book_price(session, row, found)
            if (
                row.order_type == OrderType.MARKET.value
                and found.filled_qty < row.qty
                and row.placed_at is not None
                and now - as_utc(row.placed_at) >= RESTING_AFTER
            ):
                report.resting.append((row.id, found))

    def remarket(
        self,
        session: Session,
        gtt: HatOrder,
        order: BrokerOrder,
        now: datetime,
        alert: dict[str, str],
    ) -> None:
        """A triggered GTT's sale the book shows at the market but still resting (a market sale
        under market price protection is a limit that can rest too): asked to go at the market
        again (at the protection band of the price now), unless the instrument is locked at its
        lower circuit, where it waits in the queue for a buyer. Nothing while not `enabled`."""
        if not self.enabled:
            return
        position = session.get(HatPosition, gtt.position_id)
        if position is None:
            return
        if self._locked(session, position.instrument_id, now):
            self._alert_locked(order.order_id, alert, now)
            return
        if not self._desk.market_sale(order.order_id).ok:
            return  # asked again at the next sync
        misses = self._gtt_misses[order.order_id] = self._gtt_misses.get(order.order_id, 0) + 1
        self._page_missed(misses, alert, now)

    # --- after the fill sync ----------------------------------------------------------------------

    def after_sync(
        self, session: Session, report: SyncReport, now: datetime, seller: SaleSender
    ) -> None:
        """Send again the shares of each sale HAT cancelled that has now ended; then, when
        `enabled`, cancel each market sale resting unfilled so the next sync sends it fresh.
        One sale's failure leaves the others."""
        for row_id in report.resend:
            try:
                self._send_again(session, row_id, now, seller)
            except Exception:
                session.rollback()
                logger.exception("re-send of sale %s failed", row_id)
        if not self.enabled:
            return
        for row_id, found in report.resting:
            try:
                self._cancel_resting(session, row_id, found, now, seller)
            except Exception:
                session.rollback()
                logger.exception("cancel of resting sale %s failed", row_id)

    @staticmethod
    def _send_again(session: Session, row_id: uuid.UUID, now: datetime, seller: SaleSender) -> None:
        """A fresh market sale of what the cancelled sale `row_id` left, through the exit path
        (the same session, lock and stop checks)."""
        row = session.get(HatOrder, row_id)
        if row is None or row.position_id is None:
            return
        position = session.get(HatPosition, row.position_id)
        if position is None or position.closed_at is not None:
            return
        sold = sum(session.scalars(select(HatFill.qty).where(HatFill.order_id == row.id)).all())
        qty = position.qty_open if row.purpose != EXIT_PARTIAL else row.qty - sold
        seller.sell_again(session, position, row, qty, now)

    def _cancel_resting(
        self,
        session: Session,
        row_id: uuid.UUID,
        found: BrokerOrder,
        now: datetime,
        seller: SaleSender,
    ) -> None:
        """Cancel a market sale resting unfilled so it is sent again fresh, unless its
        instrument is locked at the lower circuit (alerted once, left in the queue) or the
        re-send, a sync later, would fall after the order cutoff (left resting); the owner is
        paged at the `page_after`th miss."""
        row = session.get(HatOrder, row_id)
        if row is None or row.position_id is None or row.broker_order_id is None:
            return
        position = session.get(HatPosition, row.position_id)
        signal = session.get(HatSignal, row.signal_id)
        if position is None or signal is None:
            return
        detail = {
            "bot_id": str(signal.bot_id),
            "signal_id": str(signal.id),
            "position_id": str(position.id),
        }
        with self._desk.positions.lock(position.id, "sale_resend_cancel"):
            session.refresh(row)
            if row.status not in LIVE:
                return
            if not seller.orders_allowed(now + RESTING_AFTER):
                return  # no re-send could follow before the cutoff: the sale keeps resting
            if self._locked(session, position.instrument_id, now):
                self._alert_locked(row.tag, detail, now)
                return
            if not self._broker.cancel_order(row.broker_order_id).ok:
                return  # still resting: asked again at the next sync
            mark_resend(session, row, found, signal.bot_id, now)
            session.commit()
        self._page_missed(resends(session, signal.id, row.purpose, now), detail, now)

    # --- alerts -----------------------------------------------------------------------------------

    def _locked(self, session: Session, instrument_id: uuid.UUID, now: datetime) -> bool:
        """The instrument's last trade is at its lower circuit (False when unknown)."""
        return self._band is not None and self._band.locked(session, instrument_id, now) is True

    def _alert_locked(self, key: str, detail: dict[str, str], now: datetime) -> None:
        """`sale.unfilled` (code `lower_circuit_locked`), once per sale."""
        if key in self._locked_alerted:
            return
        self._locked_alerted.add(key)
        self._notifier.alert(
            ALERT_SALE_UNFILLED, trade_date_of(now), {**detail, "code": LOWER_CIRCUIT_LOCKED}
        )

    def _page_missed(self, misses: int, detail: dict[str, str], now: datetime) -> None:
        """Page the owner (URGENT `sale.unfilled`, code `sale_resting`) at the `page_after`th
        miss of a sale, once."""
        if misses == self._page_after:
            self._notifier.alert(
                ALERT_SALE_UNFILLED,
                trade_date_of(now),
                {**detail, "code": SALE_RESTING, "misses": misses},
            )
