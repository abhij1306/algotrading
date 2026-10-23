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

An action the rescale cannot handle (a rights issue, a demerger or any `other` action, and a
consolidation, factor 1 or above) changes no share count HAT can know: it is never applied, and
the position is manage-only for good instead (HAT-EXEC-144, design review item 6, option A):
`manage_only` holds it, so its exits see no view and its instrument takes no entry, the broker
stop is left as it is, and `ca.rescale` records one URGENT `corporate_action.unhandled` per
(position, action). The owner resolves it: a `/flatten` sells at most the shares the broker
holds, or, for a virtual position, the admin command line's `resolve-action` (PR 73) records
`corporate_action.resolved` (`resolve_action`) and closes it virtually at the price the owner
gives (the ex-date's adjusted price, PR 31 follow-ups 1 and 12).

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

from ..data.corporate_actions import PRICE_NEUTRAL_TYPES, TYPE_BONUS, TYPE_SPLIT
from ..errors import HatError
from ..events import record
from ..models import HatCorporateAction, HatEvent, HatInstrument, HatPosition, HatWatchlistEntry
from ..money import require_exact
from ..notify import Notifier
from ..time import as_utc, trade_date_of
from .botlock import lock_bot
from .owner import OwnerCommand

__all__ = [
    "ALERT_ACTION_PENDING",
    "ALERT_ACTION_UNCHECKED",
    "EVENT_RESCALED",
    "EVENT_RESOLVED",
    "EVENT_UNHANDLED",
    "ResolutionError",
    "SKIP_ACTION_PENDING",
    "Scaled",
    "applied_factors",
    "awaits_rescale",
    "exact_factor",
    "flag_unhandled",
    "held_and_watchlisted",
    "manage_only",
    "pending_actions",
    "rescale_open_positions",
    "resolve_action",
    "scale_position",
    "scale_qty",
    "unhandled_actions",
]

logger = logging.getLogger(__name__)

EVENT_RESCALED = "position.rescaled"
EVENT_UNHANDLED = "corporate_action.unhandled"
EVENT_RESOLVED = "corporate_action.resolved"
CLOSE_REASON = "corporate_action"  # the exit reason of a virtual position closed on resolution
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
    return [
        exact_factor(require_exact(p["factor"], "factor", floats=True))
        for p in _rescale_events(session, position_id)
    ]


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


def _rescalable(action: HatCorporateAction) -> bool:
    """A split or bonus of factor below 1: the only actions `ca.rescale` applies."""
    return action.action_type in RESCALED_TYPES and Decimal(action.adjustment_factor) < 1


def unhandled_actions(
    session: Session, position: HatPosition, day: date
) -> list[HatCorporateAction]:
    """The stored actions of the position's instrument the rescale cannot handle (`other`, or a
    consolidation: factor 1 or above), ex-date after the day it opened and on or before `day`,
    oldest first. A price-neutral row (a dividend, PR 47) is no such action. None for a closed
    position (HAT-EXEC-144)."""
    if position.closed_at is not None:
        return []
    opened = trade_date_of(as_utc(position.opened_at))
    actions = session.scalars(
        select(HatCorporateAction)
        .where(
            HatCorporateAction.instrument_id == position.instrument_id,
            HatCorporateAction.ex_date > opened,
            HatCorporateAction.ex_date <= day,
            HatCorporateAction.action_type.not_in(PRICE_NEUTRAL_TYPES),
        )
        .order_by(HatCorporateAction.ex_date, HatCorporateAction.action_type)
    )
    return [a for a in actions if not _rescalable(a)]


def manage_only(session: Session, position: HatPosition, day: date) -> bool:
    """True while the position spans an action it was not rescaled for, or one the rescale
    cannot handle: its exits see no view (HAT-EXEC-142, HAT-EXEC-144)."""
    return bool(
        pending_actions(session, position, day) or unhandled_actions(session, position, day)
    )


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
    """True while some open position of the instrument has an action it was not rescaled for,
    or one the rescale cannot handle: the instrument is manage-only and that position's exits
    wait (HAT-EXEC-142, HAT-EXEC-144)."""
    return any(manage_only(session, p, day) for p in _open_positions(session, instrument_id))


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


def _flagged(session: Session, position_id: uuid.UUID) -> set[str]:
    rows = session.scalars(
        select(HatEvent.payload).where(
            HatEvent.aggregate_type == "position",
            HatEvent.aggregate_id == position_id,
            HatEvent.event_type == EVENT_UNHANDLED,
        )
    )
    return {str(dict(row or {}).get("action")) for row in rows}


def flag_unhandled(session: Session, day: date, now: datetime) -> int:
    """Record one `corporate_action.unhandled` per (open position, unhandled action) not yet
    recorded; the relay sends it as an URGENT alert (HAT-EXEC-144). Returns how many were
    recorded. Works in the caller's transaction."""
    flagged = 0
    for position in _open_positions(session):
        done = _flagged(session, position.id)
        for action in unhandled_actions(session, position, day):
            key = _key(action.ex_date, action.action_type)
            if key in done:
                continue
            symbol = session.scalar(
                select(HatInstrument.symbol).where(HatInstrument.id == position.instrument_id)
            )
            record(
                session,
                aggregate_type="position",
                aggregate_id=position.id,
                event_type=EVENT_UNHANDLED,
                actor="scheduler",
                payload={
                    "action": key,
                    "factor": str(action.adjustment_factor),
                    "symbol": str(symbol or "?"),
                    "virtual": "true" if position.is_virtual else "false",
                },
                correlation_id=position.signal_id,
                bot_id=position.bot_id,
                occurred_at=now,
            )
            done.add(key)
            flagged += 1
    return flagged


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


class ResolutionError(HatError):
    """The owner's resolution of a locked position was refused; `code` names the rule."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def resolve_action(
    session: Session,
    position_id: uuid.UUID,
    action: str,
    price: Decimal | None,
    command: OwnerCommand,
) -> HatPosition:
    """The owner resolves one unhandled action (`<ex-date>:<type>`, as its
    `corporate_action.unhandled` names it) of an open virtual position (PR 73; PR 31 follow-ups
    1, 12): records `corporate_action.resolved` as the owner's (`actor = user`, the allowlisted
    chat, `via = cli`) at `price` (the ex-date's adjusted price the owner reads), and the caller
    closes the position at it in the same transaction (`trades.close_position` with
    `CLOSE_REASON`: it sells it virtually and writes its trade result). Outside market hours,
    under the bot's lock. Refused, writing nothing: inside a
    session (`market_hours`), an unknown or closed position (`position_unknown`), a real one
    (`real_position_flatten`: its quantity and stop are on the old scale, and lifting the lock
    would also lift `/flatten`'s cap at the shares the broker holds, so it is resolved by
    `/flatten`), an action that does not lock it (`action_not_unhandled`) and a missing or
    non-positive price (`close_price_required`)."""
    if command.in_session:
        raise ResolutionError("market_hours")
    position = session.get(HatPosition, position_id)
    if position is None:
        raise ResolutionError("position_unknown")
    lock_bot(session, position.bot_id)
    session.refresh(position)
    if position.closed_at is not None:
        raise ResolutionError("position_unknown")
    if not position.is_virtual:
        raise ResolutionError("real_position_flatten")
    locking = {
        _key(a.ex_date, a.action_type)
        for a in unhandled_actions(session, position, command.trade_date)
    }
    if action not in locking:
        raise ResolutionError("action_not_unhandled")
    if price is None or not price.is_finite() or price <= 0:
        raise ResolutionError("close_price_required")
    record(
        session,
        aggregate_type="position",
        aggregate_id=position.id,
        event_type=EVENT_RESOLVED,
        actor="user",
        payload={
            "action": action,
            "price": str(price),
            "chat_id": command.chat_id,
            "via": "cli",
        },
        correlation_id=position.signal_id,
        bot_id=position.bot_id,
        occurred_at=command.now,
    )
    session.flush()
    return position
