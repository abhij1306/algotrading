"""The Telegram service's rows: cursors, the command log and the sent-message links
(HAT-TG-011, HAT-TG-012, HAT-SCH-074). Each function works in the caller's transaction."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import HatCommandLog, HatTelegramCursor, HatTelegramMessage
from ..time import as_utc

__all__ = [
    "EVENTS",
    "UPDATES",
    "cursor",
    "log_command",
    "record_message",
    "set_cursor",
]

UPDATES = "updates"  # the next getUpdates offset
EVENTS = "events"  # the last hat_event id the relay has sent
_ARGS_LIMIT = 200
_COMMAND_LIMIT = 64


def cursor(session: Session, name: str) -> int:
    row = session.get(HatTelegramCursor, name)
    return int(row.position) if row is not None else 0


def set_cursor(session: Session, name: str, position: int, now: datetime) -> None:
    row = session.get(HatTelegramCursor, name)
    if row is None:
        session.add(HatTelegramCursor(name=name, position=position, updated_at=as_utc(now)))
    else:
        row.position = position
        row.updated_at = as_utc(now)
    session.flush()


def log_command(
    session: Session,
    *,
    chat_id: int,
    command: str,
    args: str | None,
    accepted: bool,
    reason: str | None,
    received_at: datetime,
) -> HatCommandLog:
    """One `hat_command_log` row. A refusal always carries a reason."""
    if not accepted and not reason:
        reason = "refused"
    row = HatCommandLog(
        chat_id=chat_id,
        command=command[:_COMMAND_LIMIT],
        args=args[:_ARGS_LIMIT] if args else None,
        accepted=accepted,
        reason=reason,
        received_at=as_utc(received_at),
    )
    session.add(row)
    session.flush()
    return row


def record_message(
    session: Session,
    *,
    chat_id: int,
    kind: str,
    ref_id: uuid.UUID | None,
    message_id: int,
    sent_at: datetime,
) -> None:
    """Link a sent message to its subject (a signal, a position, a bot)."""
    session.add(
        HatTelegramMessage(
            chat_id=chat_id,
            kind=kind,
            ref_id=ref_id,
            message_id=message_id,
            sent_at=as_utc(sent_at),
        )
    )
    session.flush()
