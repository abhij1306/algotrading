"""The per-bot lock (HAT-EXEC-045): one transaction at a time writes a bot's trades or watchlist;
and the per-owner lock (HAT-RISK-030): one entry decision at a time across all the owner's bots,
so two bots cannot both fit under the global open-risk cap on one snapshot.

On Postgres each is a transaction-scoped advisory lock keyed by the bot or the owner, so it ends
with the transaction (commit or rollback) and never outlives a crashed caller. SQLite has one
writer at a time and needs none. A caller that takes both takes the bot's first.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

__all__ = ["lock_bot", "lock_owner"]


def lock_bot(session: Session, bot_id: uuid.UUID) -> None:
    """Hold the bot's lock until the transaction ends (commit or rollback).

    Two evaluations of one bot can then never interleave: the second waits here, reads the
    snapshot after the first committed, and so sees the slot the first took. Other bots are
    unaffected. A no-op on SQLite, which runs one writer at a time."""
    _lock(session, f"hat:bot:{bot_id}")


def lock_owner(session: Session, user_id: uuid.UUID) -> None:
    """Hold the owner's entry lock until the transaction ends (commit or rollback).

    Two entries of different bots of one owner then never decide on one snapshot of all the
    owner's bots: the second waits here and sees the slot the first took. A no-op on SQLite."""
    _lock(session, f"hat:owner:{user_id}")


def _lock(session: Session, key: str) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})
