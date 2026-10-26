"""The mode queue and what the control operations share (HAT-GOV-084).

`queue_mode_change` queues a bot's mode change for the next session open, in the caller's open
transaction with its event (`events.record`); it does not commit. The kill switches are
`kill_ops.py`, the drawdown kills and the nightly snapshot `drawdown.py`, a bot's pause and its
exposure ladder `bots.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from ..configs.records import Bot
from ..configs.unit import configs_of
from ..errors import HatError
from ..events import record
from ..models import HatModeChange
from .modes import ModeChange, plan_mode_change
from .sessions import SessionCalendar

__all__ = ["ControlError", "actor_payload", "queue_mode_change"]


class ControlError(HatError):
    """A control operation was refused; the message names the rule, never a payload."""


def actor_payload(actor: str, chat_id: int | None, payload: dict) -> dict:
    """An event payload with the chat it came from when a user asked for it."""
    return {**payload, "chat_id": chat_id} if actor == "user" else payload


def queue_mode_change(
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
    """Queue a mode change for the next session open (`mode.change_requested`). Raises
    `ModeChangeError` inside market hours, for an unknown mode, or without a known next open."""
    bot = configs_of(session).get(Bot, bot_id)
    if bot is None:
        raise ControlError("unknown bot")
    if via not in ("telegram", "dashboard"):
        raise ControlError("a mode change comes through telegram or the dashboard")
    plan: ModeChange = plan_mode_change(to_mode, requested_at, calendar)
    row = HatModeChange(
        bot_id=bot_id,
        from_mode=bot.execution_mode,
        to_mode=plan.to_mode,
        requested_at=plan.requested_at,
        effective_from=plan.effective_from,
        via=via,
    )
    session.add(row)
    session.flush()
    record(
        session,
        aggregate_type="bot",
        aggregate_id=bot_id,
        event_type="mode.change_requested",
        actor=actor,
        payload=actor_payload(
            actor, chat_id, {"to_mode": to_mode, "effective_from": plan.effective_from.isoformat()}
        ),
        bot_id=bot_id,
        occurred_at=requested_at,
    )
    return row
