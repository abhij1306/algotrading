"""Open positions before a demerger (CA8, owner 2026-10-24; the rule: `data/demerger_notice.py`).

A position whose instrument has a demerger notice (NSE's `bc` file listed it, the nightly
`ca.adjust` stored it) exits at the close of the last session before the ex-date, when the
notice was read before that session (on a night before it) (`demerger_notice.exit_day`):

- a virtual position is closed at that session's close pass (15:05-15:10,
  `daily/exits.ShadowExits`) at the live price, exit reason `demerger` (`close_due`);
- a real position is never sold by HAT for it: the nightly records one URGENT
  `corporate_action.demerger_exit` telling the owner to sell on that day before continuous
  trading ends (15:15, `flag_positions`).
  The owner's sale goes through `/flatten` as any other;
- a notice read too late (after that session's close) or a position still open after it is
  recorded once as URGENT `corporate_action.demerger_late`, and the position keeps today's
  handling: its stop rests, and once the factor is stored it is manage-only
  (`rescale.unhandled_actions`).

A stored notice the night's `bc` file no longer lists before its ex-date is recorded once as
`corporate_action.demerger_unlisted` (`flag_unlisted`, Q6): it stands, and the owner checks the
record date.

Everything here works in the caller's transaction and never commits. Nothing reaches a broker.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.demerger_notice import CLOSE_REASON, exit_day, last_session_before
from ..events import record
from ..models import HatDemergerNotice, HatEvent, HatInstrument, HatPosition
from ..time import as_utc, trade_date_of
from . import shadow

__all__ = [
    "EVENT_EXIT_DUE",
    "EVENT_LATE",
    "EVENT_UNLISTED",
    "Due",
    "close_due",
    "due_positions",
    "flag_positions",
    "flag_unlisted",
]

EVENT_EXIT_DUE = "corporate_action.demerger_exit"
EVENT_LATE = "corporate_action.demerger_late"
EVENT_UNLISTED = "corporate_action.demerger_unlisted"


@dataclass(frozen=True)
class Due:
    """An open position held into a demerger notice: the session it exits at (None: the notice
    came too late) and the last session before the ex-date."""

    position: HatPosition
    notice: HatDemergerNotice
    exit_on: date | None
    last: date


def _key(notice: HatDemergerNotice) -> str:
    return f"{notice.ex_date.isoformat()}:demerger"


def due_positions(session: Session, sessions: Sequence[date]) -> list[Due]:
    """Every open position opened before an ex-date of its instrument's notices, with its exit
    session. A notice whose ex-date is past the sorted calendar `sessions` waits for a calendar
    that reaches it (its last session before the ex-date is not known yet)."""
    if not sessions:
        return []
    # positions (hat_state) and notices (hat_live) are two reads, joined here
    positions = session.scalars(
        select(HatPosition)
        .where(HatPosition.closed_at.is_(None))
        .order_by(HatPosition.opened_at, HatPosition.id)
    ).all()
    notices = session.scalars(
        select(HatDemergerNotice)
        .where(
            HatDemergerNotice.instrument_id.in_({p.instrument_id for p in positions}),
            HatDemergerNotice.ex_date <= sessions[-1],
        )
        .order_by(HatDemergerNotice.ex_date)
    ).all()
    rows = [(p, n) for p in positions for n in notices if n.instrument_id == p.instrument_id]
    found = []
    for position, notice in rows:
        last = last_session_before(sessions, notice.ex_date)
        if last is None or trade_date_of(as_utc(position.opened_at)) >= notice.ex_date:
            continue
        found.append(
            Due(position, notice, exit_day(sessions, notice.ex_date, notice.announced_on), last)
        )
    return found


def _recorded(session: Session, position_id: uuid.UUID, event_type: str) -> set[str]:
    rows = session.scalars(
        select(HatEvent.payload).where(
            HatEvent.aggregate_type == "position",
            HatEvent.aggregate_id == position_id,
            HatEvent.event_type == event_type,
        )
    )
    return {str(dict(row or {}).get("action")) for row in rows}


def flag_positions(
    session: Session, day: date, sessions: Sequence[date], now: datetime
) -> tuple[int, int]:
    """The nightly's alerts, after the night's notices are stored (night of `day`): one
    `corporate_action.demerger_exit` per real position and notice whose exit session is still
    ahead, and one `corporate_action.demerger_late` per position and notice that came too late
    or whose exit session has passed with the position open. Returns (exit, late) recorded."""
    counts = {EVENT_EXIT_DUE: 0, EVENT_LATE: 0}
    for due in due_positions(session, sessions):
        position = due.position
        if due.exit_on is not None and due.exit_on > day:
            if position.is_virtual:
                continue  # closed at the exit session's close pass
            event_type = EVENT_EXIT_DUE
        else:
            event_type = EVENT_LATE
        key = _key(due.notice)
        if key in _recorded(session, position.id, event_type):
            continue
        symbol = session.scalar(
            select(HatInstrument.symbol).where(HatInstrument.id == position.instrument_id)
        )
        record(
            session,
            aggregate_type="position",
            aggregate_id=position.id,
            event_type=event_type,
            actor="scheduler",
            payload={
                "action": key,
                "symbol": str(symbol or "?"),
                "sell_by_close_of": (due.exit_on or due.last).isoformat(),
                "announced_on": due.notice.announced_on.isoformat(),
                "virtual": "true" if position.is_virtual else "false",
            },
            correlation_id=position.signal_id,
            bot_id=position.bot_id,
            occurred_at=now,
        )
        counts[event_type] += 1
    return counts[EVENT_EXIT_DUE], counts[EVENT_LATE]


def close_due(session: Session, position: HatPosition, price: Decimal, now: datetime) -> bool:
    """Close a virtual position at its exit session's close price with exit reason `demerger`
    (`shadow.close_position`: the shadow fills' costs and slippage, its trade result). False when
    it was already closed."""
    return shadow.close_position(session, position.id, price, CLOSE_REASON, now)


def flag_unlisted(session: Session, notices: Sequence[HatDemergerNotice], now: datetime) -> int:
    """Record one `corporate_action.demerger_unlisted` per notice (on its instrument) not yet
    recorded; returns how many were. The notice is not retired (Q6)."""
    flagged = 0
    for notice in notices:
        key = _key(notice)
        recorded = session.scalars(
            select(HatEvent.payload).where(
                HatEvent.aggregate_type == "instrument",
                HatEvent.aggregate_id == notice.instrument_id,
                HatEvent.event_type == EVENT_UNLISTED,
            )
        )
        if key in {str(dict(row or {}).get("action")) for row in recorded}:
            continue
        symbol = session.scalar(
            select(HatInstrument.symbol).where(HatInstrument.id == notice.instrument_id)
        )
        record(
            session,
            aggregate_type="instrument",
            aggregate_id=notice.instrument_id,
            event_type=EVENT_UNLISTED,
            actor="scheduler",
            payload={
                "action": key,
                "symbol": str(symbol or "?"),
                "announced_on": notice.announced_on.isoformat(),
            },
            occurred_at=now,
        )
        flagged += 1
    return flagged
