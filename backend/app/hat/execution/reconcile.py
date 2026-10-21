"""Reconciliation of the broker's book with HAT's (HAT-BRK-110, -111, -112; HAT-OPS-003; 07 §11).

On startup, before the first order of a session, and periodically, the broker's HT-tagged orders
and the positions it holds are matched to `hat_order` and `hat_position`. Nothing is placed until a
reconciliation has succeeded (`ExecutionGuard.reconciled`). A mismatch never auto-closes or
auto-adopts a position: it blocks new entries for the affected bot (the whole account when the
bot cannot be told) and raises one alert per mismatch, while exits and stops keep running. Orders
tagged by the terminal (`ST`) or untagged are out of scope. The model is
`verification/tla/OrderReconciliation.tla`.

Delivery positions: the broker shows a same-day CNC buy in positions and a settled one in
holdings. The quantity compared is the larger of the two per symbol (a same-day buy that appears
in both is not counted twice); a partial overlap reads as a mismatch, which fails closed.
[verify] on a live account (owner step, PLAN Q8).
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

from ..models import HatBot, HatInstrument, HatOrder, HatPosition, HatSignal
from ..notify import Notifier
from ..time import trade_date_of, utc_now
from .broker import BrokerHolding, BrokerOrder, BrokerPosition, HatBroker
from .guard import ExecutionGuard
from .submit import adopt_broker_order
from .tags import is_hat_tag

__all__ = [
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
    """Quantity held per symbol: the larger of the CNC position and the holding."""
    in_positions: dict[str, int] = defaultdict(int)
    for position in positions:
        if position.product == "CNC":
            in_positions[position.symbol] += position.qty
    in_holdings: dict[str, int] = defaultdict(int)
    for holding in holdings:
        in_holdings[holding.symbol] += holding.qty
    return {
        symbol: max(in_positions.get(symbol, 0), in_holdings.get(symbol, 0))
        for symbol in {*in_positions, *in_holdings}
    }


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
    alerts: int  # new alerts raised by this run


AlertSink = Callable[[Mismatch], None]


def _log_alert(mismatch: Mismatch) -> None:
    logger.error("reconcile mismatch kind=%s", mismatch.kind)


MISMATCH_ALERT = "reconcile.mismatch"


def _today() -> date:
    return trade_date_of(utc_now())


def notifier_sink(notifier: Notifier, today: Callable[[], date] = _today) -> AlertSink:
    """The reconciliation's alert sink over a `Notifier` (Telegram from PR 16, the log until
    then): one `reconcile.mismatch` health alert per new mismatch, carrying its kind, the tag or
    symbol it is about and the bot (`account` when the bot cannot be told). Fixed words only,
    never broker text."""

    def alert(mismatch: Mismatch) -> None:
        bot = str(mismatch.bot_id) if mismatch.bot_id is not None else "account"
        notifier.alert(
            MISMATCH_ALERT, today(), {"kind": mismatch.kind, "key": mismatch.key, "bot_id": bot}
        )

    return alert


class ReconciliationService:
    """Run it at startup and then periodically; it owns the one-alert-per-mismatch memory."""

    def __init__(
        self, broker: HatBroker, guard: ExecutionGuard, alert: AlertSink = _log_alert
    ) -> None:
        self._broker = broker
        self._guard = guard
        self._alert = alert
        self._alerted: set[tuple[str, str, uuid.UUID | None]] = set()

    def run(self, session: Session, account_id: uuid.UUID) -> ReconcileReport:
        """Reconcile one broker account. Raises BrokerUnavailableError (leaving the guard
        unreconciled, so nothing is placed) when the broker cannot be read."""
        self._guard.begin_session()
        orders = self._broker.orders()
        quantities = broker_quantities(self._broker.positions(), self._broker.holdings())
        by_tag = {o.tag: o for o in orders if is_hat_tag(o.tag)}

        rows = session.execute(
            select(HatOrder).where(HatOrder.broker_account_id == account_id)
        ).scalars()
        hat_orders = [row for row in rows if not row.is_virtual]
        adopted = []
        for row in hat_orders:
            if row.status in ("pending", "unconfirmed") and row.tag in by_tag:
                bot_id = session.execute(
                    select(HatSignal.bot_id).where(HatSignal.id == row.signal_id)
                ).scalar_one_or_none()
                adopt_broker_order(session, row, by_tag[row.tag], bot_id)
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
            reconciled=True, blocked_bots=blocked_bots, blocked_accounts=blocked_accounts
        )
        return ReconcileReport(mismatches=mismatches, adopted=adopted, alerts=len(fresh))
