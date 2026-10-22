"""Open positions across a split or bonus (HAT-EXEC-140..143; audit F3).

A split or bonus with factor f (a 1:2 split is 0.5, a 1:1 bonus 0.5, a 1:2 bonus 2/3) multiplies
every price before its ex-date by f. A position opened before the ex-date and still open on it
holds prices of the old scale: unchanged, its stop sits above the new price and `hard_stop` sells
it on the ex-date open for nothing but the action. So:

- the night before, `ca.adjust` stores the next session's actions of the held and watchlisted
  instruments (`daily/nightly.py`), and the features read them;
- before the open, `ca.rescale` (`rescale_open_positions`) rescales each open position of such an
  instrument: quantity ⌊q / f⌋ (a fractional entitlement is the broker's cash, not shares),
  average entry and both stops × f, in one transaction with the event `position.rescaled` that
  records the old and new values. The `hat_position_stop_guard` trigger lets `current_stop` move
  down only by such an event, recorded in the same transaction (HAT-SCH-081);
- the broker stop follows down to the rescaled stop and quantity (`StopDesk.rescaled`);
- until a position is rescaled it `awaits_rescale`: its exit rules see no view (no exit fires
  from the action alone) and its instrument takes no entry (`corporate_action_pending`), and
  the job alerts `corporate_action.pending`.

Everything here works in the caller's transaction and never commits. Nothing reaches a broker.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.corporate_actions import TYPE_BONUS, TYPE_SPLIT
from ..events import record
from ..models import HatCorporateAction, HatEvent, HatInstrument, HatPosition, HatWatchlistEntry
from ..notify import Notifier
from ..time import as_utc, trade_date_of
from .botlock import lock_bot

__all__ = [
    "ALERT_ACTION_PENDING",
    "ALERT_ACTION_UNCHECKED",
    "EVENT_RESCALED",
    "SKIP_ACTION_PENDING",
    "Scaled",
    "applied_factors",
    "awaits_rescale",
    "exact_factor",
    "held_and_watchlisted",
    "pending_actions",
    "rescale_open_positions",
    "scale_position",
    "scale_qty",
]

logger = logging.getLogger(__name__)

EVENT_RESCALED = "position.rescaled"
ALERT_ACTION_PENDING = "corporate_action.pending"
ALERT_ACTION_UNCHECKED = "corporate_action.unchecked"  # a next-session lookup failed
SKIP_ACTION_PENDING = "corporate_action_pending"
MAX_DENOMINATOR = 1000  # split and bonus ratios are small: 2/3 is stored as 0.666666666667
# Two ratios with denominators up to 1000 differ by at least 1e-6, so a stored factor within 1e-9
# of one (12 places, or the 10 a SQLite Numeric returns) names it alone.
FACTOR_TOLERANCE = Fraction(1, 10**9)
RESCALED_TYPES = (TYPE_SPLIT, TYPE_BONUS)
_MONEY = Decimal("0.0001")  # hat_position prices are Numeric(18, 4)


@dataclass(frozen=True)
class Scaled:
    qty_open: int
    avg_entry: Decimal
    initial_stop: Decimal
    current_stop: Decimal

    def to_json(self) -> dict[str, str | int]:
        return {
            "qty_open": self.qty_open,
            "avg_entry": str(self.avg_entry),
            "initial_stop": str(self.initial_stop),
            "current_stop": str(self.current_stop),
        }


def exact_factor(stored: Decimal) -> Fraction:
    """The ratio a stored factor stands for (2/3 from 0.666666666667). A factor no ratio with a
    denominator up to 1000 rounds to (0.6667, sent to 4 places) raises ValueError: a guessed
    ratio would size the position wrongly, so it stays manage-only and is alerted instead."""
    value = Fraction(stored)
    ratio = value.limit_denominator(MAX_DENOMINATOR)
    if abs(ratio - value) > FACTOR_TOLERANCE:
        raise ValueError("corporate-action factor is not an exact small ratio")
    return ratio


def scale_qty(qty: int, factor: Fraction) -> int:
    """⌊qty / f⌋: the shares held after the action (a fraction is paid out in cash)."""
    return int(Fraction(qty) / factor) if qty > 0 else 0


def _price(value: Decimal, factor: Fraction, rounding: str) -> Decimal:
    exact = Decimal(factor.numerator) * value / Decimal(factor.denominator)
    return exact.quantize(_MONEY, rounding)


def scale_position(
    qty_open: int, avg_entry: Decimal, initial_stop: Decimal, current_stop: Decimal, f: Fraction
) -> Scaled:
    """The position after an action of factor `f`: quantity ⌊q / f⌋, prices × f. Stops round
    down to the stored places (never above the exact stop) and both the same way, so
    `current_stop >= initial_stop` still holds."""
    return Scaled(
        qty_open=scale_qty(qty_open, f),
        avg_entry=_price(avg_entry, f, ROUND_HALF_EVEN),
        initial_stop=_price(initial_stop, f, ROUND_FLOOR),
        current_stop=_price(current_stop, f, ROUND_FLOOR),
    )


def _rescale_events(session: Session, position_id: uuid.UUID) -> list[dict]:
    rows = session.scalars(
        select(HatEvent.payload)
        .where(
            HatEvent.aggregate_type == "position",
            HatEvent.aggregate_id == position_id,
            HatEvent.event_type == EVENT_RESCALED,
        )
        .order_by(HatEvent.id)
    )
    return [dict(row or {}) for row in rows]


def applied_factors(session: Session, position_id: uuid.UUID) -> list[Fraction]:
    """The factors already applied to the position, oldest first."""
    return [exact_factor(Decimal(str(p["factor"]))) for p in _rescale_events(session, position_id)]


def _key(ex_date: date, action_type: str) -> str:
    return f"{ex_date.isoformat()}:{action_type}"


def pending_actions(session: Session, position: HatPosition, day: date) -> list[HatCorporateAction]:
    """The stored splits and bonuses (factor below 1) of the position's instrument it has not
    been rescaled for: ex-date after the day it opened and on or before `day`, oldest first. None
    for a closed position. Other actions (rights, demergers, `other`) and consolidations change
    no share count HAT can know, so they are not rescaled (HAT-EXEC-141)."""
    if position.closed_at is not None:
        return []
    opened = trade_date_of(as_utc(position.opened_at))
    done = {str(p.get("action")) for p in _rescale_events(session, position.id)}
    actions = session.scalars(
        select(HatCorporateAction)
        .where(
            HatCorporateAction.instrument_id == position.instrument_id,
            HatCorporateAction.ex_date > opened,
            HatCorporateAction.ex_date <= day,
            HatCorporateAction.action_type.in_(RESCALED_TYPES),
            HatCorporateAction.adjustment_factor < 1,
        )
        .order_by(HatCorporateAction.ex_date, HatCorporateAction.action_type)
    )
    return [a for a in actions if _key(a.ex_date, a.action_type) not in done]


def _open_positions(session: Session, instrument_id: uuid.UUID | None = None) -> list[HatPosition]:
    query = select(HatPosition).where(HatPosition.closed_at.is_(None))
    if instrument_id is not None:
        query = query.where(HatPosition.instrument_id == instrument_id)
    return list(session.scalars(query.order_by(HatPosition.opened_at, HatPosition.id)))


def held_and_watchlisted(session: Session, day: date) -> dict[str, uuid.UUID]:
    """Symbol to instrument id of every instrument with an open position or a watchlist row of
    `day` (the names the next session's watchlist mostly repeats; HAT-EXEC-140)."""
    ids = {p.instrument_id for p in _open_positions(session)}
    ids |= set(
        session.scalars(
            select(HatWatchlistEntry.instrument_id).where(HatWatchlistEntry.trade_date == day)
        )
    )
    if not ids:
        return {}
    rows = session.execute(
        select(HatInstrument.symbol, HatInstrument.id).where(HatInstrument.id.in_(ids))
    )
    return {str(symbol): iid for symbol, iid in rows.all()}


def awaits_rescale(session: Session, instrument_id: uuid.UUID, day: date) -> bool:
    """True while some open position of the instrument has an action it was not rescaled for:
    the instrument is manage-only and that position's exits wait (HAT-EXEC-142)."""
    return any(pending_actions(session, p, day) for p in _open_positions(session, instrument_id))


def _apply(
    session: Session, position: HatPosition, action: HatCorporateAction, now: datetime
) -> None:
    f = exact_factor(Decimal(action.adjustment_factor))
    before = Scaled(
        position.qty_open,
        Decimal(position.avg_entry),
        Decimal(position.initial_stop),
        Decimal(position.current_stop),
    )
    after = scale_position(
        before.qty_open, before.avg_entry, before.initial_stop, before.current_stop, f
    )
    # The event first: the stop guard reads it to let `current_stop` move down.
    record(
        session,
        aggregate_type="position",
        aggregate_id=position.id,
        event_type=EVENT_RESCALED,
        actor="scheduler",
        payload={
            "action": _key(action.ex_date, action.action_type),
            "factor": str(action.adjustment_factor),
            "from": before.to_json(),
            "to": after.to_json(),
        },
        correlation_id=position.signal_id,
        bot_id=position.bot_id,
        occurred_at=now,
    )
    position.qty_open = after.qty_open
    position.avg_entry = after.avg_entry
    position.initial_stop = after.initial_stop
    position.current_stop = after.current_stop
    session.flush()


def rescale_position(session: Session, position_id: uuid.UUID, day: date, now: datetime) -> int:
    """Apply every pending action of one position under its bot's lock; how many were."""
    position = session.get(HatPosition, position_id)
    if position is None:
        return 0
    lock_bot(session, position.bot_id)
    session.refresh(position)
    actions = pending_actions(session, position, day)
    for action in actions:
        _apply(session, position, action, now)
    return len(actions)


def rescale_open_positions(
    session_factory: Callable[[], Session], day: date, now: datetime, notifier: Notifier
) -> tuple[int, Sequence[uuid.UUID]]:
    """The `ca.rescale` body: every open position with a pending action, each in its own
    transaction. Returns (actions applied, positions that failed); each failure is alerted
    `corporate_action.pending` and the position stays manage-only."""
    with session_factory() as session:
        ids = [p.id for p in _open_positions(session) if pending_actions(session, p, day)]
    applied = 0
    failed: list[uuid.UUID] = []
    for position_id in ids:
        try:
            with session_factory() as session:
                applied += rescale_position(session, position_id, day, now)
                session.commit()
        except Exception:
            logger.exception("rescaling position %s failed", position_id)
            failed.append(position_id)
            notifier.alert(ALERT_ACTION_PENDING, day, {"position_id": str(position_id)})
    return applied, failed
