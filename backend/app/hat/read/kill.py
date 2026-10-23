"""The kill-switch rows and the manage-only reasons derived from them (`risk.kill` rules)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.orm import Session

from ..models import HatKillSwitch
from ..risk import kill

__all__ = ["account_tripped", "kill_rows", "manage_only_reasons", "reasons_of"]

Key = tuple[str, uuid.UUID | None]  # (scope, bot); the account's bot is None


def reasons_of(stream: str) -> tuple[str, ...]:
    """The reasons whose rows make up `stream` (`kill.stream_of`)."""
    return (stream,) if stream in kill.HEALTH_REASONS else kill.RISK_REASONS


def kill_rows(session: Session, *, keys: Iterable[Key] | None = None) -> list[kill.KillRow]:
    """The latest `hat_kill_switch` row of each (scope, bot, stream), of every key or of `keys`:
    the rows `kill.current_state` reads a state from (each stream is its latest row; on a tie a
    tripped row wins), so the read stays as long as the streams, however often they flap."""
    stream = case(
        (HatKillSwitch.reason.in_(kill.HEALTH_REASONS), HatKillSwitch.reason),
        else_=literal("risk"),
    )
    occurred = case(
        (
            and_(HatKillSwitch.state == kill.ARMED, HatKillSwitch.rearmed_at.is_not(None)),
            HatKillSwitch.rearmed_at,
        ),
        else_=HatKillSwitch.tripped_at,
    )
    rank = (
        func.row_number()
        .over(
            partition_by=(HatKillSwitch.scope, HatKillSwitch.bot_id, stream),
            order_by=(occurred.desc(), (HatKillSwitch.state == kill.TRIPPED).desc()),
        )
        .label("rank")
    )
    ranked = select(HatKillSwitch.id, rank)
    if keys is not None:
        ranked = ranked.where(
            or_(
                *(
                    and_(
                        HatKillSwitch.scope == scope,
                        HatKillSwitch.bot_id.is_(None)
                        if bot_id is None
                        else HatKillSwitch.bot_id == bot_id,
                    )
                    for scope, bot_id in keys
                )
            )
        )
    latest = ranked.subquery()
    return [
        kill.KillRow(r.scope, r.bot_id, r.state, r.tripped_at, r.reason, r.rearmed_at)
        for r in session.scalars(
            select(HatKillSwitch)
            .join(latest, latest.c.id == HatKillSwitch.id)
            .where(latest.c.rank == 1)
        )
    ]


def account_tripped(rows: list[kill.KillRow]) -> bool:
    """Whether any stream of the account key is tripped (either channel)."""
    return kill.current_state(rows, "account", None) == kill.TRIPPED


def manage_only_reasons(state: kill.KillState) -> tuple[str, ...]:
    """Why a bot takes no entry: `account_kill`, `bot_kill`, `health_kill`, `paused`, in that
    order; empty when it may enter."""
    flags = (
        ("account_kill", state.account_tripped),
        ("bot_kill", state.bot_tripped),
        ("health_kill", state.health_tripped),
        ("paused", state.paused),
    )
    return tuple(name for name, on in flags if on)
