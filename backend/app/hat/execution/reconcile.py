"""Reconciliation of the broker's book with HAT's (HAT-BRK-110, -111, -112; HAT-OPS-003).

On startup, before the first order of a session, and periodically, the broker's HT-tagged orders
and the positions it holds are matched to `hat_order` and `hat_position`. No entry is placed on a
trade date until a reconciliation of that date has succeeded (`ExecutionGuard.reconciled_for`);
stops and sales never wait for it (D118). A mismatch never auto-closes or auto-adopts a position:
it blocks new entries for the affected bot (the whole account when the bot cannot be told) and
raises one alert per mismatch, while exits and stops keep running. Orders tagged by the terminal
(`ST`) or untagged are out of scope: such a row the adapter cannot read is skipped and counted,
with one `reconcile.unreadable_row` health alert per trade date. The model is
`verification/tla/OrderReconciliation.tla`.

Delivery positions: the broker shows a settled delivery in holdings and today's CNC trades as a
signed net position. The quantity compared is the holding plus today's signed net CNC quantity
per symbol (MX-F8): a buy today adds, a sale today of a holding subtracts, so a holding sold in
full reads as 0. A same-day buy shown in both would count twice and read as a mismatch, which
fails closed. [verify] on a live account (owner step).
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import SYSTEM_CLOCK, Clock
from ..models import HatBot, HatInstrument, HatOrder, HatPosition, HatSignal
from ..notify import Notifier
from ..time import trade_date_of
from .broker import BrokerHolding, BrokerOrder, BrokerPosition, HatBroker
from .guard import ExecutionGuard
from .submit import TagBook, adopt_broker_order
from .tags import is_hat_tag

__all__ = [
    "DUPLICATE_HT_ORDER",
    "HatPositionView",
    "Mismatch",
    "ReconcileReport",
    "ReconciliationService",
    "broker_quantities",
    "find_mismatches",
    "notifier_sink",
]

logger = logging.getLogger(__name__)

UNKNOWN_HT_ORDER = "unknown_ht_order"
POSITION_WITHOUT_HAT_ROW = "broker_position_without_hat_row"
POSITION_MISSING_AT_BROKER = "hat_position_missing_at_broker"
QUANTITY_DIFFERENCE = "quantity_difference"
DUPLICATE_HT_ORDER = "duplicate_ht_order"  # Fyers does not enforce tag uniqueness (HAT-BRK-103)


@dataclass(frozen=True)
class Mismatch:
    """One difference. `bot_id` None means the bot cannot be told: the whole account is blocked."""

    kind: str
    key: str  # the tag or symbol it is about
    bot_id: uuid.UUID | None


@dataclass(frozen=True)
class HatPositionView:
    bot_id: uuid.UUID
    symbol: str
    qty_open: int


def broker_quantities(
    positions: Iterable[BrokerPosition], holdings: Iterable[BrokerHolding]
) -> dict[str, int]:
    """Quantity held per symbol: the holding plus today's signed net CNC position."""
    held: dict[str, int] = defaultdict(int)
    for position in positions:
        if position.product == "CNC":
            held[position.symbol] += position.qty
    for holding in holdings:
        held[holding.symbol] += holding.qty
    return dict(held)


def find_mismatches(
    *,
    broker_orders: Iterable[BrokerOrder],
    broker_qty: dict[str, int],
    hat_tags: set[str],
    hat_positions: Iterable[HatPositionView],
    hat_symbols: set[str],
) -> list[Mismatch]:
    """The mismatches between the broker's book and HAT's. Depends on the broker orders only
    through the HT-tagged ones (HAT-BRK-112)."""
    ht_orders = [o for o in broker_orders if is_hat_tag(o.tag)]
    mismatches = [
        Mismatch(UNKNOWN_HT_ORDER, str(o.tag), None) for o in ht_orders if o.tag not in hat_tags
    ]
    # Two broker orders under one tag: one logical order reached the broker twice. The bot
    # cannot be told from the order book, so the account is blocked.
    per_tag: dict[str, int] = defaultdict(int)
    for order in ht_orders:
        per_tag[str(order.tag)] += 1
    mismatches.extend(
        Mismatch(DUPLICATE_HT_ORDER, tag, None)
        for tag, count in sorted(per_tag.items())
        if count > 1
    )
    positions = [p for p in hat_positions if p.qty_open > 0]
    held = {p.symbol for p in positions}
    touched = held | hat_symbols | {o.symbol for o in ht_orders}
    # The broker holds one quantity per symbol for the whole account: compare it with the sum of
    # every bot's open quantity, and when several bots share the symbol and the sums differ the
    # bot cannot be told (the account is blocked).
    by_symbol: dict[str, list[HatPositionView]] = defaultdict(list)
    for position in positions:
        by_symbol[position.symbol].append(position)
    for symbol, views in by_symbol.items():
        quantity = broker_qty.get(symbol, 0)
        bot_ids = {view.bot_id for view in views}
        sole_bot = next(iter(bot_ids)) if len(bot_ids) == 1 else None
        if quantity <= 0:
            mismatches.extend(
                Mismatch(POSITION_MISSING_AT_BROKER, symbol, b) for b in sorted(bot_ids, key=str)
            )
        elif quantity != sum(view.qty_open for view in views):
            mismatches.append(Mismatch(QUANTITY_DIFFERENCE, symbol, sole_bot))
    mismatches.extend(
        Mismatch(POSITION_WITHOUT_HAT_ROW, symbol, None)
        for symbol, quantity in sorted(broker_qty.items())
        if quantity > 0 and symbol in touched and symbol not in held
    )
    return mismatches


@dataclass(frozen=True)
class ReconcileReport:
    mismatches: list[Mismatch]
    adopted: list[str]  # tags of pending or unconfirmed orders found at the broker
    alerts: int  # new mismatch alerts raised by this run
    skipped_rows: int = 0  # order rows outside HAT's scope that could not be read


AlertSink = Callable[[Mismatch], None]


def _log_alert(mismatch: Mismatch) -> None:
    logger.error("reconcile mismatch kind=%s", mismatch.kind)


MISMATCH_ALERT = "reconcile.mismatch"
UNREADABLE_ROW_ALERT = "reconcile.unreadable_row"


def notifier_sink(notifier: Notifier, clock: Clock = SYSTEM_CLOCK) -> AlertSink:
    """The reconciliation's alert sink over a `Notifier` (Telegram from PR 16, the log until
    then): one `reconcile.mismatch` health alert per new mismatch, dated by `clock`, carrying
    its kind, the tag or symbol it is about and the bot (`account` when the bot cannot be told).
    Fixed words only, never broker text."""

    def alert(mismatch: Mismatch) -> None:
        bot = str(mismatch.bot_id) if mismatch.bot_id is not None else "account"
        notifier.alert(
            MISMATCH_ALERT,
            trade_date_of(clock.now()),
            {"kind": mismatch.kind, "key": mismatch.key, "bot_id": bot},
        )

    return alert


class ReconciliationService:
    """Run it at startup and then periodically; it owns the one-alert-per-mismatch memory."""

    def __init__(
        self,
        broker: HatBroker,
        guard: ExecutionGuard,
        alert: AlertSink = _log_alert,
        notifier: Notifier | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._broker = broker
        self._guard = guard
        self._clock = clock  # stamps the orders it adopts
        self._alert = alert
        self._notifier = notifier
        self._alerted: set[tuple[str, str, uuid.UUID | None]] = set()
        self._unreadable_alerted_on: date | None = None

    def run(self, session: Session, account_id: uuid.UUID) -> ReconcileReport:
        """Reconcile one broker account. Raises BrokerUnavailableError when the broker cannot be
        read, leaving the guard as it was: a trade date not yet reconciled places no entry, and a
        routine re-run never closes the gate a success of the same date opened. A success opens
        the entries of the guard's trade date (`ExecutionGuard.update`)."""
        day = self._guard.today()  # the date of the book read, not of the run's end
        read = self._guard.begin_read()
        orders = self._broker.orders()
        skipped = orders.skipped
        self._alert_unreadable(day, skipped)
        quantities = broker_quantities(self._broker.positions(), self._broker.holdings())
        book = TagBook.of(orders)

        rows = session.execute(
            select(HatOrder).where(HatOrder.broker_account_id == account_id)
        ).scalars()
        hat_orders = [row for row in rows if not row.is_virtual]
        adopted = []
        for row in hat_orders:
            found = book.for_row(row) if row.status in ("pending", "unconfirmed") else None
            if found is not None:
                bot_id = session.execute(
                    select(HatSignal.bot_id).where(HatSignal.id == row.signal_id)
                ).scalar_one_or_none()
                adopt_broker_order(session, row, found, bot_id, clock=self._clock)
                adopted.append(row.tag)
        session.commit()

        bots = {
            bot_id
            for (bot_id,) in session.execute(
                select(HatBot.id).where(HatBot.broker_account_id == account_id)
            )
        }
        symbols = dict(
            session.execute(
                select(HatPosition.id, HatInstrument.symbol).join(
                    HatInstrument, HatInstrument.id == HatPosition.instrument_id
                )
            ).all()
        )
        positions = [
            HatPositionView(p.bot_id, symbols[p.id], p.qty_open)
            for p in session.execute(
                select(HatPosition).where(
                    HatPosition.bot_id.in_(bots),
                    HatPosition.closed_at.is_(None),
                    HatPosition.is_virtual.is_(False),
                )
            ).scalars()
        ]
        hat_symbols = set(
            session.execute(
                select(HatInstrument.symbol)
                .join(HatSignal, HatSignal.instrument_id == HatInstrument.id)
                .join(HatOrder, HatOrder.signal_id == HatSignal.id)
                .where(HatOrder.broker_account_id == account_id, HatOrder.is_virtual.is_(False))
            ).scalars()
        )
        mismatches = find_mismatches(
            broker_orders=orders,
            broker_qty=quantities,
            hat_tags={row.tag for row in hat_orders},
            hat_positions=positions,
            hat_symbols=hat_symbols,
        )

        blocked_bots = {m.bot_id for m in mismatches if m.bot_id is not None}
        blocked_accounts = {account_id} if any(m.bot_id is None for m in mismatches) else set()
        current = {(m.kind, m.key, m.bot_id) for m in mismatches}
        fresh = [m for m in mismatches if (m.kind, m.key, m.bot_id) not in self._alerted]
        for mismatch in fresh:
            self._alert(mismatch)
        self._alerted = current  # a mismatch that clears and returns alerts again
        self._guard.update(
            read=read, day=day, blocked_bots=blocked_bots, blocked_accounts=blocked_accounts
        )
        return ReconcileReport(
            mismatches=mismatches, adopted=adopted, alerts=len(fresh), skipped_rows=skipped
        )

    def _alert_unreadable(self, day: date, skipped: int) -> None:
        """One `reconcile.unreadable_row` health alert per trade date with a skipped row."""
        if skipped == 0 or self._unreadable_alerted_on == day:
            return
        self._unreadable_alerted_on = day
        logger.warning("reconcile skipped %d unreadable order rows", skipped)
        if self._notifier is not None:
            self._notifier.alert(UNREADABLE_ROW_ALERT, day, {"rows": skipped})
