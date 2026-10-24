"""Open positions before a demerger (CA8, owner 2026-10-24; the rule: `data/demerger_notice.py`).

A position whose instrument has a demerger notice (NSE's `bc` file listed it, the nightly
`ca.adjust` stored it) exits at the close of the last session before the ex-date, when the
notice was read on or before that session (`demerger_notice.exit_day`):

- a virtual position is closed at that session's close pass (15:20-15:25,
  `daily/exits.ShadowExits`) at the live price, exit reason `demerger` (`close_due`);
- a real position is never sold by HAT for it: the nightly records one URGENT
  `corporate_action.demerger_exit` telling the owner to sell by that close (`flag_positions`).
  The owner's sale goes through `/flatten` as any other;
- a notice read too late (after that session's close) or a position still open after it is
  recorded once as URGENT `corporate_action.demerger_late`, and the position keeps today's
  handling: its stop rests, and once the factor is stored it is manage-only
  (`rescale.unhandled_actions`).

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
    "Due",
    "close_due",
    "due_positions",
    "flag_positions",
]

EVENT_EXIT_DUE = "corporate_action.demerger_exit"
EVENT_LATE = "corporate_action.demerger_late"


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
    rows = session.execute(
        select(HatPosition, HatDemergerNotice)
        .join(HatDemergerNotice, HatDemergerNotice.instrument_id == HatPosition.instrument_id)
        .where(HatPosition.closed_at.is_(None), HatDemergerNotice.ex_date <= sessions[-1])
        .order_by(HatPosition.opened_at, HatPosition.id, HatDemergerNotice.ex_date)
    ).all()
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
