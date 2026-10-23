"""The kill-switch rows and the manage-only reasons derived from them (`risk.kill` rules)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HatKillSwitch
from ..risk import kill

__all__ = ["account_tripped", "kill_rows", "manage_only_reasons"]


def kill_rows(session: Session) -> list[kill.KillRow]:
    """Every `hat_kill_switch` row."""
    return [
        kill.KillRow(r.scope, r.bot_id, r.state, r.tripped_at, r.reason, r.rearmed_at)
        for r in session.scalars(select(HatKillSwitch))
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
