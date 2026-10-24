"""Mode changes: queued outside market hours for the next session open, settled at that open
(HAT-GOV-084, HAT-GOV-085, HAT-SCH-086). The only code that writes a bot's mode; no API
route reaches it (CONF `auto_mode_single_user`). The reads are `modes.py`."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..events import record
from ..models import HatBot, HatBrokerAccount, HatModeChange
from ..risk import controls
from ..risk.modes import plan_mode_change
from ..risk.sessions import SessionCalendar
from ..time import as_utc
from .modes import (
    ModeRefusedError,
    account_gates,
    gate_refusal,
    request_mode,
    sleeve_refusal,
)

__all__ = ["apply_mode_changes", "request_mode_change"]


def request_mode_change(
    session: Session,
    *,
    bot_id: uuid.UUID,
    to_mode: str,
    requested_at: datetime,
    calendar: SessionCalendar,
    via: str,
    actor: str = "user",
    chat_id: int | None = None,
) -> HatModeChange:
    """Queue a mode change for the next session open. Refused with ModeRefusedError when a gate
    would keep the mode from running at that open (`gate_refusal`) or the bot's sleeve is not
    Live (`sleeve_refusal`, Q9), and with `ModeChangeError` inside market hours; nothing is
    written when refused."""
    bot = session.get(HatBot, bot_id)
    if bot is None:
        raise controls.ControlError("unknown bot")
    account = session.get(HatBrokerAccount, bot.broker_account_id)
    opens = plan_mode_change(to_mode, requested_at, calendar).effective_from
    request_mode(to_mode, account_gates(session, account, opens))
    refusal = sleeve_refusal(to_mode, bot.lifecycle_state)
    if refusal is not None:
        raise ModeRefusedError(refusal)
    return controls.queue_mode_change(
        session,
        bot_id=bot_id,
        to_mode=to_mode,
        requested_at=requested_at,
        calendar=calendar,
        via=via,
        actor=actor,
        chat_id=chat_id,
    )


def _due(session: Session, bot_id: uuid.UUID, now: datetime) -> list[HatModeChange]:
    rows = session.scalars(
        select(HatModeChange).where(
            HatModeChange.bot_id == bot_id,
            HatModeChange.effective_from <= as_utc(now),
            HatModeChange.applied_at.is_(None),
            HatModeChange.refused_reason.is_(None),
        )
    ).all()
    return sorted(rows, key=lambda r: (as_utc(r.effective_from), as_utc(r.requested_at)))


def apply_mode_changes(session: Session, now: datetime) -> int:
    """At a session open, settle every queued change now due (in the caller's transaction), in
    the order queued: one whose mode passes the gates as they stand at `now` (and whose sleeve
    is Live, for confirm and auto) is written to
    `hat_bot.execution_mode` (`mode.applied`); one that does not is refused with the gate's code
    and never takes effect (`mode.apply_refused`), so the bot keeps its mode. Idempotent: a
    settled change is not read again. Returns how many bots changed mode."""
    changed = 0
    for bot in session.scalars(select(HatBot)):
        before = bot.execution_mode
        account = session.get(HatBrokerAccount, bot.broker_account_id)
        gates = account_gates(session, account, now)
        for row in _due(session, bot.id, now):
            refusal = gate_refusal(row.to_mode, gates) or sleeve_refusal(
                row.to_mode, bot.lifecycle_state
            )
            payload = {"from_mode": bot.execution_mode, "to_mode": row.to_mode}
            if refusal is None:
                row.applied_at = as_utc(now)
                bot.execution_mode = row.to_mode
                kind = "mode.applied"
            else:
                row.refused_reason = refusal
                payload["reason"] = refusal
                kind = "mode.apply_refused"
            record(
                session,
                aggregate_type="bot",
                aggregate_id=bot.id,
                event_type=kind,
                actor="scheduler",
                payload={**payload, "change_id": str(row.id)},
                bot_id=bot.id,
                occurred_at=now,
            )
        changed += bot.execution_mode != before
    session.flush()
    return changed
