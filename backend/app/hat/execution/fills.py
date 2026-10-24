"""Broker fills into HAT's book (HAT-EXEC-060, -062, HAT-BRK-111; PR 15c).

`FillSync.run` reads the broker's order book and trade book (both fail closed: an unreadable
book raises and nothing is written) and then, for one broker account, runs its phases in order:
**settle** (steps 1-5: HAT's book only, no broker call, no position lock), **commit**, **market**
(6b) and **protect** (6, 6c, 7). The phases after the commit start from what it returns, so no
position lock is ever taken over an uncommitted fill (`PositionDesk`).

1. adopts a pending or unconfirmed HAT order the broker shows under its tag
   (`submit.adopt_broker_order`), so a fill never lands on an order HAT thinks was not sent;
2. records each new trade of an HT-tagged order as a fill (`live_trades.apply_entry_fill`,
   `apply_exit_fill`);
3. attributes the SELL trade of a triggered GTT to that GTT: one under the GTT's own tag
   (`gtt_tag`: the adapter reads a row under one of HAT's GTT tags as that GTT's sale; whether
   Fyers puts the tag on the sale is [verify on a live account]) to the working GTT with that
   tag, and an untagged one to the GTT of the one open position on its symbol; a broker trade id
   is recorded once across all orders, so an old trade never closes a newer position. Each
   position a fill moved is protected in the protect phase, before anything else is done to it:
   an entry fill gets its broker stop (HAT-EXEC-060), a sale resizes the stop, and the last sale
   cancels it;
4. moves order statuses (`submit.record_broker_status`): an entry that ended unfilled ends its
   signal Cancelled; a day stop that expired leaves its position to be protected again; a sale
   the broker took and then ended short records `order.rejected` and fires its rules again, so the
   next evaluation retries it once; its second rejection raises `exit.not_placed` (HAT-EXEC-075);
5. raises `gtt.unfilled` for such a GTT's SELL order (by its tag, or untagged on its symbol) that
   ended (cancelled, rejected, expired) short of its quantity: the GTT triggered and did not fill
   .
   The GTT is ended and the position protected again. The broker order id is kept on the
   `order.cancelled` event, so the alert is raised once; while the GTT's own sale (at its limit
   price and quantity) still works short of its quantity a minute after it was first seen (the
   price gapped below the limit), it rests: `gtt.triggered` is recorded and `gtt.unfilled`
   raised once, and the sale is asked to go at the market (the market phase) at every sync until
   the book shows it does (HAT-EXEC-066);
6. protects again every open real position of the account that has no stop the broker
   confirmed (a stop the broker refused twice was alerted; a stop whose answer was lost is
   settled by `StopDesk.protect`; each sync tries again);
6b. the market phase: market price protection (PR 48, `mpp.MarketProtection.read_book`) reads
   HAT's sales in the book: the price each shows, each market sale resting unfilled
   (`SyncReport.resting`) and each HAT cancelled to send again that has now ended
   (`SyncReport.resend`), for the `TradeManager` to send again; then each triggered GTT's resting
   sale is asked to go at the market (one already at the market is `MarketProtection.remarket`'s);
6c. (P1, HAT-EXEC-145) every `gtt_check_every` (`HAT_GTT_CHECK_MINUTES`, default 5), and on
   every run the 08:40 `gtt.check` asks for, reads the broker's GTT book before the order book
   and compares it with the GTTs HAT believes rest (`stop_plans.stop_confirmed`). A GTT a good read
   no longer holds, with no sale of it in the order book (one that triggered is the fill sync's
   to attribute), was cancelled or expired by the broker: `GttUpkeep.check_gtt` ends it, alerts it
   once (`gtt.missing`) and protects the position again at once, so the engine's own hard stop
   is no longer held back for it. A GTT book that cannot be read changes nothing and is alerted
   once (`gtt.unverified`, code `gtt_book_unread`) until a read succeeds;
7. closes each position its sales emptied while its entry still worked, once the broker's book
   shows the entry ended or its trade date passed (`live_trades.close_if_emptied`); while it
   still works, the cancel of its remainder is asked unless it was already (F2). A late fill of
   the remainder lands on the open position in step 2 and is protected like any entry fill.

Each fill and its stop work commit as separate steps (the model's atomic steps, StopPlacement).
Several fills of one position in one sync get one protect, after all of them are recorded.

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
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..clock import SYSTEM_CLOCK, Clock
from ..engine import live_trades
from ..engine.positions import TradeError
from ..events import record
from ..models import HatBot, HatEvent, HatFill, HatInstrument, HatOrder, HatPosition, HatSignal
from ..notify import LogNotifier, Notifier
from ..time import trade_date_of
from .broker import (
    BrokerOrder,
    BrokerTrade,
    BrokerUnavailableError,
    HatBroker,
    OrderType,
    Side,
)
from .gtt_upkeep import GttUpkeep
from .order_book import (
    EXIT_SALES,
    MAX_SALE_REJECTIONS,
    TagBook,
    adopt_broker_order,
    record_broker_status,
    rejected_sales,
)
from .protect import (
    ALERT_EXIT_NOT_PLACED,
    ALERT_GTT_UNFILLED,
    ALERT_GTT_UNVERIFIED,
    EVENT_GTT_TRIGGERED,
    EXIT_WORKING,
    FAILED,
    UNVERIFIED,
    StopDesk,
)
from .stop_plans import LIVE, STOP_DAY, STOP_GTT, UNSURE, WORKING, stop_confirmed

if TYPE_CHECKING:
    from .mpp import MarketProtection

__all__ = [
    "ALERT_FILL_EXCESS",
    "ALERT_FILL_REFUSED",
    "ALERT_SALE_UNFILLED",
    "RESTING_AFTER",
    "FillSync",
    "SaleBand",
    "SyncReport",
    "watch_closely",
]

logger = logging.getLogger(__name__)

GTT_CHECK_EVERY = timedelta(minutes=5)  # the GTT book read by the fill sync (P1)

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
# A sale working this long is resting, not on its way to a fill (HAT-EXEC-066, PR 48).
RESTING_AFTER = timedelta(seconds=60)
# PR 48: a sale the price left unfilled (market price protection, the band-limit sale).
ALERT_SALE_UNFILLED = "sale.unfilled"


class SaleBand(Protocol):
    """The day's lower price band of an instrument, read from the stored band (PR 51)."""

    def lower(self, session: Session, instrument_id: uuid.UUID, now: datetime) -> Decimal | None:
        """The lower circuit price; None when the band or previous close is unknown."""
        ...

    def locked(self, session: Session, instrument_id: uuid.UUID, now: datetime) -> bool | None:
        """Whether the last trade is at the lower circuit; None when unknown."""
        ...


def watch_closely(session: Session, account_id: uuid.UUID, now: datetime) -> bool:
    """Whether the account's fills are read at every periodic step (HAT-EXEC-060): a real entry of
    its signals of this trade date may still fill (one of an earlier session ended at its close),
    or a stop of an open real position was sent and its answer is not in yet (pending or
    unconfirmed: the next sync settles it). A stop the broker refused is retried once a minute,
    never faster, so a refusal does not spend the tag's sequence."""
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
    # PR 48: HAT's market sales resting unfilled a minute after their send, with the book's row;
    # and those HAT cancelled to send again that ended in this sync.
    resting: list[tuple[uuid.UUID, BrokerOrder]] = field(default_factory=list)
    resend: list[uuid.UUID] = field(default_factory=list)
    # P1: the GTT ids of the GTT book this sync read, and when (None: not read, or unreadable),
    # and the positions whose dropped GTT could not be replaced yet.
    gtt_book: frozenset[str] | None = None
    gtt_read_at: datetime | None = None
    gtt_unprotected: list[uuid.UUID] = field(default_factory=list)


@dataclass
class _SyncPass:
    """One fill sync's reads, and what its settle phase found for the phases after it."""

    account_id: uuid.UUID
    now: datetime
    orders: list[BrokerOrder]
    trades: list[BrokerTrade]
    rows: dict[str, HatOrder]
    book: TagBook
    report: SyncReport
    # positions to protect, in the order touched; True once a fill touched it (`_protect`
    # counts it protected)
    touched: dict[uuid.UUID, bool] = field(default_factory=dict)
    ended: list[HatOrder] = field(default_factory=list)  # rows whose status the broker moved
    resting: list[tuple[HatOrder, BrokerOrder, dict[str, str]]] = field(default_factory=list)
    excess: list[tuple[str, int, int]] = field(default_factory=list)  # (tag, qty, filled)

    def touch(self, position_id: uuid.UUID, *, fill: bool) -> None:
        self.touched[position_id] = self.touched.get(position_id, False) or fill


@dataclass(frozen=True)
class _Settled:
    """The settle phase's work once committed: the phases that take a position lock
    (`PositionDesk.lock`) or call the broker start from this only, so none of them can run over
    an uncommitted fill or status."""

    sync: _SyncPass


class FillSync:
    """Fills for one broker account's session; `desk` places and moves its stops, `upkeep` (the
    account's one `GttUpkeep`, shared with its `TradeManager`) checks its GTTs."""

    def __init__(
        self,
        broker: HatBroker,
        desk: StopDesk,
        notifier: Notifier | None = None,
        *,
        mpp: MarketProtection,
        upkeep: GttUpkeep,
        gtt_check_every: timedelta = GTT_CHECK_EVERY,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._broker = broker
        self._desk = desk
        self._upkeep = upkeep
        self._clock = clock  # stamps the orders it adopts
        self._notifier = notifier or LogNotifier()
        self.mpp = mpp  # market price protection (PR 48), shared with the `TradeManager`
        self._refused_trades: set[str] = set()
        self._unmarketed: set[str] = set()  # resting GTT sales whose conversion was alerted
        self._working_since: dict[str, datetime] = {}  # a GTT's sale first seen working
        self._gtt_check_every = gtt_check_every
        self._gtts_read_at: datetime | None = None  # the last good GTT-book read (by `now`)
        self._gtt_unread_alerted = False  # until a read succeeds again

    def run(
        self, session: Session, account_id: uuid.UUID, now: datetime, *, check_gtts: bool = False
    ) -> SyncReport:
        """One pass over the broker's books, in phases: read, settle (steps 1-5: database
        writes only), commit, market (6b), protect (6, 6c, 7). Raises BrokerUnavailableError
        when the order or the trade book cannot be read (nothing is written then); an unreadable
        GTT book only skips step 6c. `check_gtts` reads the GTT book whether or not it is due."""
        sync = self._read(session, account_id, now, check_gtts=check_gtts)
        self._settle(session, sync)
        settled = self._commit(session, sync)
        self._market(session, settled)
        self._protect_all(session, settled)
        return sync.report

    # --- the phases -----------------------------------------------------------------------------

    def _read(
        self, session: Session, account_id: uuid.UUID, now: datetime, *, check_gtts: bool
    ) -> _SyncPass:
        """Read the books: the GTT book when due, then the order book and the trade book."""
        report = SyncReport()
        if check_gtts or self._gtts_due(now):
            # read before the order book: a GTT that triggered before this read has its sale there
            report.gtt_read_at = self._clock.now()
            report.gtt_book = self._read_gtts(account_id, now)
        orders = list(self._broker.orders())
        trades = list(self._broker.trades())
        # Every step reaches a row through a tag the broker's books show today (`TagBook.for_row`
        # and the trades match on the tag), so only those rows are read, not the account's whole
        # history (DB audit L1).
        tags = {o.tag for o in orders if o.tag} | {t.tag for t in trades if t.tag}
        rows = {
            row.tag: row
            for row in session.scalars(
                select(HatOrder).where(
                    HatOrder.broker_account_id == account_id,
                    HatOrder.is_virtual.is_(False),
                    HatOrder.tag.in_(tags),
                )
            )
        }
        return _SyncPass(account_id, now, orders, trades, rows, TagBook.of(orders), report)

    def _settle(self, session: Session, sync: _SyncPass) -> None:
        """Steps 1-5 into HAT's book: adoptions, fills, statuses and the triggered GTTs' sales.
        No broker call and no position lock; the positions they touch are protected later."""
        self._adopt(session, sync)
        self._trades(session, sync)
        self._statuses(session, sync)
        self._gtt_sales(session, sync)

    @staticmethod
    def _commit(session: Session, sync: _SyncPass) -> _Settled:
        """Commit what the settle phase wrote (each step commits its own, so this is the
        barrier, not the only commit): a position lock is never taken over an uncommitted row
        (`PositionDesk`)."""
        session.commit()
        return _Settled(sync)

    def _market(self, session: Session, settled: _Settled) -> None:
        """Step 6b: market price protection reads HAT's sales in the book
        (`MarketProtection.read_book`), then each triggered GTT's resting sale is asked to go at
        the market (HAT-EXEC-066)."""
        sync = settled.sync
        self.mpp.read_book(
            session, sync.rows.values(), sync.ended, sync.book, sync.now, sync.report
        )
        for gtt, order, alert in sync.resting:
            self._market_gtt_sale(session, gtt, order, sync.now, alert)

    def _protect_all(self, session: Session, settled: _Settled) -> None:
        """Steps 6, 6c and 7: every position the settle phase touched is protected (an entry fill
        gets its stop before anything else touches the position, HAT-EXEC-060), then the GTT
        book is checked, then every position still without a stop is protected, a closed one's
        unsure stops are cancelled and an emptied one closed."""
        sync = settled.sync
        account_id, now, report = sync.account_id, sync.now, sync.report
        for position_id, fill in sync.touched.items():
            if fill:
                self._protect(session, position_id, now, report)
            else:
                # A stop that ended (a day stop expired at the close), a sale that ended short or
                # a GTT that triggered unfilled: the position must still rest on a broker stop.
                self._desk.protect(session, position_id, now)
        for tag, qty, filled in sync.excess:
            self._alert_excess(tag, qty, filled, now)  # after the stop
        if report.gtt_book is not None and report.gtt_read_at is not None:
            self._check_gtts(session, sync, report.gtt_book, report.gtt_read_at)
            self._gtts_read_at = now
        for position_id in self._unprotected(session, account_id) - report.protected:
            self._protect(session, position_id, now, report)
        for position_id in self._closed_with_unsure_stops(session, account_id) - report.protected:
            # A sale may close a position while one of its stops is still unsure (HAT-EXEC-065):
            # once settled, a stop that landed is cancelled, so it never rests under no shares.
            self._desk.cancel_all(session, position_id, now)
        for position_id in self._emptied(session, account_id):
            self._close_emptied(session, position_id, now)

    def _trades(self, session: Session, sync: _SyncPass) -> None:
        """Steps 2 and 3: each trade of an HT-tagged order, then each sale of a triggered GTT."""
        for trade in sync.trades:
            row = sync.rows.get(trade.tag) if trade.tag else None
            if row is not None:
                self._apply(session, row, trade, sync)
        gtts = self._working_gtts(session, sync.account_id)
        for trade in sync.trades:
            gtt = gtts.of(trade)
            if gtt is not None and trade.side is Side.SELL:
                self._apply_gtt_trade(session, gtt, trade, sync)

    def _gtt_sales(self, session: Session, sync: _SyncPass) -> None:
        """Step 5 over the order book: each triggered GTT's sale that ended short or rests."""
        gtts = self._working_gtts(session, sync.account_id)
        for order in sync.orders:
            gtt = gtts.of(order)
            if gtt is None:
                continue
            if self._unfilled_trigger(order, gtt):
                self._gtt_unfilled(session, gtt, order, sync)
            elif self._resting_trigger(session, order, gtt):
                self._gtt_resting(session, gtt, order, sync)
        # a sale that filled or left the book before it counted as resting is forgotten
        working = {order.order_id for order in sync.orders}
        self._working_since = {
            order_id: since
            for order_id, since in self._working_since.items()
            if order_id in working
        }

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

    def _adopt(self, session: Session, sync: _SyncPass) -> None:
        for row in sync.rows.values():
            found = sync.book.for_row(row) if row.status in UNSURE else None
            if found is not None:
                adopt_broker_order(
                    session, row, found, self._bot_of(session, row), clock=self._clock
                )
                session.commit()
                sync.report.adopted += 1
                self._sale_ended(session, row, found, sync.now)

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

    def _apply(self, session: Session, row: HatOrder, trade: BrokerTrade, sync: _SyncPass) -> None:
        position_id = self._record(session, row, trade, sync.now, sync.report)
        if position_id is not None:
            # HAT-EXEC-060: the stop comes before any other action on the position.
            sync.touch(position_id, fill=True)
            if row.purpose == ENTRY:
                self._check_excess(session, row, trade.qty, sync)  # alerted after the stop

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

    @staticmethod
    def _check_excess(session: Session, row: HatOrder, qty: int, sync: _SyncPass) -> None:
        """The fill of `qty` just recorded took the entry's fills above its quantity:
        `fill.excess`, once, on the fill that crosses it (each fill is recorded once; later fills
        of the same excess are not alerted again), once the stop is placed. The fills stay
        recorded: shares refused here would sit at the broker untracked, with no stop; recorded,
        the stop covers them and reconciliation blocks the account's entries."""
        filled = int(
            session.scalar(select(func.sum(HatFill.qty)).where(HatFill.order_id == row.id)) or 0
        )
        if filled - qty <= row.qty < filled:
            sync.excess.append((row.tag, row.qty, filled))

    def _alert_excess(self, tag: str, qty: int, filled: int, now: datetime) -> None:
        logger.error("entry %s filled %s of %s", tag, filled, qty)
        self._notifier.alert(
            ALERT_FILL_EXCESS, trade_date_of(now), {"tag": tag, "qty": qty, "filled": filled}
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
        self, session: Session, gtt: HatOrder, trade: BrokerTrade, sync: _SyncPass
    ) -> None:
        seen = session.scalar(select(HatFill.id).where(HatFill.broker_trade_id == trade.trade_id))
        if seen is not None:
            return  # recorded before, on this or an older GTT
        position_id = self._record(session, gtt, trade, sync.now, sync.report)
        if position_id is None:
            return
        filled = sum(session.scalars(select(HatFill.qty).where(HatFill.order_id == gtt.id)).all())
        session.refresh(gtt)
        if gtt.status in LIVE:
            # The GTT triggered: it no longer rests as a stop, so it is marked before the
            # position is protected (a part-filled sale may still sell the rest).
            gtt.status = "filled" if filled >= gtt.qty else "partially_filled"
            session.commit()
        sync.touch(position_id, fill=True)

    def _protect(
        self, session: Session, position_id: uuid.UUID, now: datetime, report: SyncReport
    ) -> None:
        # A position whose sale still works is not counted as protected: once that sale ends
        # (later in this sync, or the next) it is protected again.
        if self._desk.protect(session, position_id, now) != EXIT_WORKING:
            report.protected.add(position_id)

    # --- 6c. the GTT book ---------------------------------------------------------------------

    def _gtts_due(self, now: datetime) -> bool:
        """The last GTT check is `gtt_check_every` old (a sync that failed did not check)."""
        return self._gtts_read_at is None or now - self._gtts_read_at >= self._gtt_check_every

    def _read_gtts(self, account_id: uuid.UUID, now: datetime) -> frozenset[str] | None:
        """The ids of the GTTs the broker's book shows, or None (alerted once) when it cannot be
        read; an unread book is read again at the next sync."""
        try:
            book = frozenset(gtt.gtt_id for gtt in self._broker.gtts())
        except BrokerUnavailableError as exc:
            if not self._gtt_unread_alerted:
                self._gtt_unread_alerted = True
                self._notifier.alert(
                    ALERT_GTT_UNVERIFIED,
                    trade_date_of(now),
                    {"account_id": str(account_id), "code": "gtt_book_unread"},
                )
            logger.warning("GTT book of account %s unread: %s", account_id, exc.code)
            return None
        self._gtt_unread_alerted = False
        return book

    def _check_gtts(
        self, session: Session, sync: _SyncPass, book: frozenset[str], read_at: datetime
    ) -> None:
        """Step 6c: each position with a live GTT the book read at `read_at` no longer holds
        and no sale of it in the order book is checked and protected again by the desk."""
        report = sync.report
        gtts = self._working_gtts(session, sync.account_id)
        triggered = {gtt.id for order in sync.orders if (gtt := gtts.of(order)) is not None}
        gone = {
            row.position_id
            for row, _symbol in gtts.by_tag.values()
            if row.id not in triggered
            and row.position_id is not None
            and stop_confirmed(row, book, read_at, self._desk.positions.settle_after) is False
        }
        for position_id in sorted(gone):
            outcome = self._upkeep.check_gtt(session, position_id, sync.now, book, read_at)
            if outcome in (FAILED, UNVERIFIED):
                report.gtt_unprotected.append(position_id)
            else:
                report.protected.add(position_id)

    # --- 4. statuses ----------------------------------------------------------------------------

    def _statuses(self, session: Session, sync: _SyncPass) -> None:
        for row in sync.rows.values():
            found = None if row.status in UNSURE else sync.book.for_row(row)
            if found is None:
                continue
            session.refresh(row)
            if record_broker_status(session, row, found, self._bot_of(session, row)):
                session.commit()
                sync.report.statuses += 1
                sync.ended.append(row)
                self._sale_ended(session, row, found, sync.now)
                if row.position_id is not None and row.purpose != ENTRY:
                    sync.touch(row.position_id, fill=False)

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
        self, session: Session, gtt: HatOrder, order: BrokerOrder, sync: _SyncPass
    ) -> None:
        now = sync.now
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
        sync.report.unfilled_gtts += 1
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
        sync.touch(position.id, fill=False)

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
        self, session: Session, gtt: HatOrder, order: BrokerOrder, sync: _SyncPass
    ) -> None:
        """HAT-EXEC-066: the GTT triggered and its limit sale still works (the price gapped below
        the limit). Once the sale has worked `RESTING_AFTER` (a sale filling at once is left to
        fill), `gtt.triggered` is recorded and `gtt.unfilled` raised; from then on every sync
        asks for the sale to go at the market (`_market_gtt_sale`) until the book shows it does.
        The GTT row stays live: its sale holds the shares, so no other stop or sale is placed
        over it."""
        now = sync.now
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
            sync.report.resting_gtts += 1
            self._notifier.alert(ALERT_GTT_UNFILLED, trade_date_of(now), {**alert, "code": RESTING})
        sync.resting.append((gtt, order, alert))

    def _market_gtt_sale(
        self,
        session: Session,
        gtt: HatOrder,
        order: BrokerOrder,
        now: datetime,
        alert: dict[str, str],
    ) -> None:
        """Ask a triggered GTT's resting sale to go at the market; a refusal is alerted once."""
        if order.order_type is OrderType.MARKET:
            # Already at the market; under market price protection it can rest too (PR 48).
            self.mpp.remarket(session, gtt, order, now, alert)
            return
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
