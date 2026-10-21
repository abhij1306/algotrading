"""The per-bot lock (HAT-EXEC-045): one transaction at a time writes a bot's trades or watchlist.

On Postgres it is a transaction-scoped advisory lock keyed by the bot, so it ends with the
transaction (commit or rollback) and never outlives a crashed caller. SQLite has one writer at a
time and needs none.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

__all__ = ["lock_bot"]


def lock_bot(session: Session, bot_id: uuid.UUID) -> None:
    """Hold the bot's lock until the transaction ends (commit or rollback).

    Two evaluations of one bot can then never interleave: the second waits here, reads the
    snapshot after the first committed, and so sees the slot the first took. Other bots are
    unaffected. A no-op on SQLite, which runs one writer at a time."""
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"hat:bot:{bot_id}"},
    )
