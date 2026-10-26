"""The bots: their labels, mode and kill state, read in one pass for every bot."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from ..configs.records import Bot, Strategy
from ..configs.unit import configs_of
from ..data.calendar import session_calendar
from ..execution.modes import ModeView, mode_view
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
    """Each bot's strategy name, else the start of its id (a bot that does not exist is left
    out)."""
    configs = configs_of(session)
    wanted = set(bot_ids)
    out = {}
    for bot in configs.all(Bot, lambda b: b.id in wanted):
        strategy = configs.get(Strategy, bot.strategy_id)
        out[bot.id] = strategy.name if strategy is not None else str(bot.id)[:8]
    return out


def label_of(session: Session, bot: Bot) -> str:
    return labels(session, (bot.id,))[bot.id]


def all_bots(session: Session) -> list[Bot]:
    return configs_of(session).all(Bot)


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
