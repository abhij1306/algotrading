"""The bots: their labels, mode and kill state, read in one pass for every bot."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.calendar import session_calendar
from ..execution.modes import ModeView, mode_view
from ..models import HatBot, HatStrategy
from ..risk import kill
from .kill import kill_rows, manage_only_reasons

__all__ = ["ACTIVE", "BotRow", "all_bots", "bot_rows", "label_of", "labels"]

ACTIVE = "active"  # `hat_bot.status` of a bot that is not paused


@dataclass(frozen=True)
class BotRow:
    id: uuid.UUID
    label: str
    status: str
    lifecycle_state: str
    risk_cluster: str
    mode: ModeView
    kill: kill.KillState
    reasons: tuple[str, ...]  # `manage_only_reasons(kill)`


def labels(session: Session, bot_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Each bot's strategy name, else the start of its id, read in one query (a bot that does
    not exist is left out)."""
    rows = session.execute(
        select(HatBot.id, HatStrategy.name)
        .outerjoin(HatStrategy, HatStrategy.id == HatBot.strategy_id)
        .where(HatBot.id.in_(set(bot_ids)))
    )
    return {bot_id: name or str(bot_id)[:8] for bot_id, name in rows}


def label_of(session: Session, bot: HatBot) -> str:
    return labels(session, (bot.id,))[bot.id]


def all_bots(session: Session) -> list[HatBot]:
    return list(session.scalars(select(HatBot).order_by(HatBot.id)))


def bot_rows(
    session: Session, now: datetime, rows: list[kill.KillRow] | None = None
) -> list[BotRow]:
    """Every bot, ordered by id, with its mode at `now` and its kill state (`rows` when the
    caller already read them)."""
    rows = kill_rows(session) if rows is None else rows
    bots = all_bots(session)
    names = labels(session, (bot.id for bot in bots))
    calendar = session_calendar(session)
    out = []
    for bot in bots:
        state = kill.kill_state_of(rows, bot.id, paused=bot.status != ACTIVE)
        out.append(
            BotRow(
                id=bot.id,
                label=names[bot.id],
                status=bot.status,
                lifecycle_state=bot.lifecycle_state,
                risk_cluster=bot.risk_cluster,
                mode=mode_view(session, bot, now, calendar),
                kill=state,
                reasons=manage_only_reasons(state),
            )
        )
    return out
