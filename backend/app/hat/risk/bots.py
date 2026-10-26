"""A bot's pause and its exposure ladder as database operations (HAT-RISK-040, HAT-GOV-081).

Every function changes state in the caller's open transaction (with its event through
`events.record`); none commits. The ladder's rule is the pure one in `exposure.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from ..configs.records import Bot
from ..configs.unit import configs_of
from ..events import record
from ..models import HatExposureState
from ..time import utc_now
from .controls import actor_payload
from .exposure import ClosedTrade, LadderState, apply_trade
from .policy import RiskPolicy

__all__ = ["ladder_state", "pause_bot", "record_closed_trade", "resume_bot"]


def pause_bot(
    session: Session,
    bot_id: uuid.UUID,
    now: datetime,
    *,
    actor: str = "user",
    chat_id: int | None = None,
) -> bool:
    """Pause an active bot: manage-only at once, `bot.paused` in the same transaction.
    False when it was not active."""
    bot = configs_of(session).get(Bot, bot_id)
    if bot is None or bot.status != "active":
        return False
    bot.status = "paused"
    record(
        session,
        aggregate_type="bot",
        aggregate_id=bot_id,
        event_type="bot.paused",
        actor=actor,
        payload=actor_payload(actor, chat_id, {}),
        bot_id=bot_id,
        occurred_at=now,
    )
    return True


def resume_bot(
    session: Session,
    bot_id: uuid.UUID,
    now: datetime,
    *,
    actor: str = "user",
    chat_id: int | None = None,
) -> bool:
    """Resume a paused bot (`bot.resumed`). It never lifts a kill switch: the kills are rows of
    their own, and `kill_state` still reads them. False when it was not paused."""
    bot = configs_of(session).get(Bot, bot_id)
    if bot is None or bot.status != "paused":
        return False
    bot.status = "active"
    record(
        session,
        aggregate_type="bot",
        aggregate_id=bot_id,
        event_type="bot.resumed",
        actor=actor,
        payload=actor_payload(actor, chat_id, {}),
        bot_id=bot_id,
        occurred_at=now,
    )
    return True


def record_closed_trade(
    session: Session, bot_id: uuid.UUID, r_multiple: Decimal, is_virtual: bool, policy: RiskPolicy
) -> LadderState:
    """Move the bot's ladder for one closed trade, in the caller's transaction (the lifecycle
    calls this where it writes `hat_trade_result`). A virtual trade changes nothing."""
    row = session.get(HatExposureState, bot_id)
    if row is None:
        row = HatExposureState(bot_id=bot_id, level="L1", consecutive_wins=0, consecutive_losses=0)
        session.add(row)
    state = LadderState(row.level, row.consecutive_wins, row.consecutive_losses)
    after = apply_trade(state, ClosedTrade(r_multiple, is_virtual), policy)
    if after != state:
        row.level = after.level
        row.consecutive_wins = after.consecutive_wins
        row.consecutive_losses = after.consecutive_losses
        row.updated_at = utc_now()
    return after


def ladder_state(session: Session, bot_id: uuid.UUID) -> LadderState:
    row = session.get(HatExposureState, bot_id)
    if row is None:
        return LadderState()
    return LadderState(row.level, row.consecutive_wins, row.consecutive_losses)
