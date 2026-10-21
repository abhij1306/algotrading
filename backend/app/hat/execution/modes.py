"""Which mode a bot runs in, and the mode-change service (HAT-DEC-B1, HAT-DEC-I9, HAT-EXEC-011,
HAT-EXEC-012, HAT-GOV-084, HAT-SCH-086; 07 §1, 06 §8).

Two rules decide the mode an entry is routed by:

- **The session's mode.** A change is queued outside market hours and takes effect at the next
  session open (`risk.modes.plan_mode_change`). During a session the mode in force is the one at
  that session's open, so nothing done mid-session changes how an entry is routed.
- **The capability gate.** Confirm and auto place real orders, and every real fill needs an
  overnight broker-side stop, so both need an active account whose capabilities report GTT for
  delivery; without it the bot runs shadow. The Fyers adapter reports none until the owner
  verifies GTT on a live account (PLAN Q8), so the MVP runs shadow only.

This module reads the database and the calendar only. It imports no broker, so the engine may
use it (CONF `brk.import_boundary`).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import HatError
from ..models import HatBot, HatBrokerAccount, HatModeChange
from ..risk import controls
from ..risk.modes import ModeChange
from ..risk.modes import effective_mode as queued_mode
from ..risk.sessions import SessionCalendar
from ..time import as_utc, trade_date_of

__all__ = [
    "AUTO",
    "CONFIRM",
    "MODES",
    "SHADOW",
    "ModeRefusedError",
    "UnknownModeError",
    "apply_mode_changes",
    "bot_mode",
    "effective_mode",
    "mode_anchor",
    "request_mode",
    "request_mode_change",
    "requested_mode",
]

SHADOW = "shadow"
CONFIRM = "confirm"
AUTO = "auto"
MODES = (SHADOW, CONFIRM, AUTO)
ACCOUNT_ACTIVE = "active"


class ModeRefusedError(HatError):
    """A mode change the account's capabilities do not allow."""


class UnknownModeError(ModeRefusedError):
    """A mode request outside shadow, confirm and auto."""


def _gtt_ready(account_status: str, capabilities: Mapping[str, object]) -> bool:
    return account_status == ACCOUNT_ACTIVE and capabilities.get("gtt_delivery") is True


def effective_mode(
    requested: str, *, account_status: str, capabilities: Mapping[str, object]
) -> str:
    """The mode a bot runs in: confirm and auto fall back to shadow unless the account is
    active and reports GTT for delivery."""
    if requested not in MODES:
        return SHADOW
    if requested != SHADOW and not _gtt_ready(account_status, capabilities):
        return SHADOW
    return requested


def request_mode(requested: str, *, account_status: str, capabilities: Mapping[str, object]) -> str:
    """Accept a mode request or refuse it: UnknownModeError for a mode outside the three,
    ModeRefusedError when the account cannot run it."""
    if requested not in MODES:
        raise UnknownModeError("unknown mode")
    if (
        effective_mode(requested, account_status=account_status, capabilities=capabilities)
        != requested
    ):
        raise ModeRefusedError("mode needs an active account with GTT for delivery")
    return requested


def mode_anchor(calendar: SessionCalendar | None, at: datetime) -> datetime:
    """The instant whose queued mode is in force at `at`: the open of `at`'s session once it has
    opened, else `at` itself (before the open, on a closed day, or with no calendar)."""
    if calendar is None:
        return as_utc(at)
    day = calendar.days.get(trade_date_of(at))
    if day is not None and day.is_trading and day.open_at is not None:
        opened = as_utc(day.open_at)
        if opened <= as_utc(at):
            return opened
    return as_utc(at)


def _changes(session: Session, bot_id: uuid.UUID) -> list[ModeChange]:
    rows = session.scalars(select(HatModeChange).where(HatModeChange.bot_id == bot_id))
    return [ModeChange(r.to_mode, r.requested_at, r.effective_from) for r in rows]


def requested_mode(session: Session, bot: HatBot, at: datetime) -> str:
    """The mode the user asked for that is in force at `at`: the latest queued change effective
    by then, else the bot's stored mode; anything outside the three modes reads as shadow."""
    mode = queued_mode(bot.execution_mode, _changes(session, bot.id), at)
    return mode if mode in MODES else SHADOW


def bot_mode(session: Session, bot: HatBot, at: datetime, calendar: SessionCalendar | None) -> str:
    """The effective mode of `bot` for an entry at `at`: the queued mode in force at the open of
    `at`'s session, gated by the account's GTT capability (a missing account is shadow)."""
    requested = requested_mode(session, bot, mode_anchor(calendar, at))
    account = session.get(HatBrokerAccount, bot.broker_account_id)
    if account is None:
        return SHADOW
    return effective_mode(
        requested, account_status=account.status, capabilities=account.capabilities or {}
    )


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
    """Queue a mode change for the next session open. Refused with ModeRefusedError when the
    bot's account cannot run the mode (confirm and auto need GTT for delivery), and with
    `ModeChangeError` inside market hours; nothing is written when refused."""
    bot = session.get(HatBot, bot_id)
    if bot is None:
        raise controls.ControlError("unknown bot")
    account = session.get(HatBrokerAccount, bot.broker_account_id)
    request_mode(
        to_mode,
        account_status=account.status if account is not None else "",
        capabilities=(account.capabilities or {}) if account is not None else {},
    )
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


def apply_mode_changes(session: Session, now: datetime) -> int:
    """At a session open, write each bot's queued mode that is now in force to
    `hat_bot.execution_mode` (in the caller's transaction). Idempotent: a second run changes
    nothing. Returns how many bots changed."""
    changed = 0
    for bot in session.scalars(select(HatBot)):
        mode = requested_mode(session, bot, now)
        if mode != bot.execution_mode:
            bot.execution_mode = mode
            changed += 1
    return changed
