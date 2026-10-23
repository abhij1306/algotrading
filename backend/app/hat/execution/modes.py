"""Which mode a bot runs in, and the mode-change service (HAT-DEC-B1, HAT-DEC-B2, HAT-DEC-I9,
HAT-EXEC-011, HAT-EXEC-012, HAT-EXEC-014, HAT-GOV-084, HAT-GOV-085, HAT-SCH-086).

A bot asks for confirm unless the owner chose otherwise (HAT-DEC-B2). Four rules decide the mode
an entry is routed by:

- **The sleeve's lifecycle.** Only a Live sleeve places real orders: every other lifecycle state
  routes as shadow whatever mode was asked (HAT-PORT-063, `lifecycle.routes_as_shadow`).

- **The session's mode.** A change is queued outside market hours and takes effect at the next
  session open (`risk.modes.plan_mode_change`). During a session the mode in force is the one at
  that session's open, so nothing done mid-session changes how an entry is routed.
- **The gates.** Confirm and auto place real orders, so both need an active account whose
  capabilities report GTT for delivery (every real fill needs an overnight broker-side stop,
  HAT-EXEC-012), the owner's word that DDPI is registered on the account (a CNC sale needs no
  TPIN) and a policy whose base weights and cluster cap the owner has confirmed
  (`weights_confirmed`); without any of them the bot runs shadow (HAT-GOV-085). DDPI and
  the policy are read as they stood at the session's open, so neither widens a mode mid-session;
  the account's GTT is read now, so losing it narrows at once.
- **Confidence.** Auto places only a high-confidence signal; any other goes to the owner as an
  approval card, as in confirm (HAT-EXEC-014, D149).

This module reads the database and the calendar only and writes nothing: queuing and applying
a change live in `mode_changes.py`, which no API route reaches (CONF `auto_mode_single_user`).
It imports no broker, so the engine may use it (CONF `brk.import_boundary`).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..engine.portfolio import lifecycle
from ..errors import HatError
from ..models import HatBot, HatBrokerAccount, HatModeChange, HatRiskPolicyVersion
from ..models.strategy import ACCOUNT_ACTIVE
from ..risk.modes import ModeChange
from ..risk.modes import effective_mode as queued_mode
from ..risk.policy import PolicyError, PolicyVersion, effective_version, parse_policy
from ..risk.sessions import SessionCalendar
from ..time import as_utc, trade_date_of

__all__ = [
    "AUTO",
    "CONFIRM",
    "DDPI_NOT_CONFIRMED",
    "HIGH",
    "MODES",
    "NO_GTT_DELIVERY",
    "NOT_HIGH_CONFIDENCE",
    "SHADOW",
    "WEIGHTS_NOT_CONFIRMED",
    "Gates",
    "ModeRefusedError",
    "UnknownModeError",
    "account_gates",
    "bot_mode",
    "effective_mode",
    "gate_refusal",
    "ModeView",
    "mode_anchor",
    "mode_view",
    "pending_change",
    "real_orders_allowed",
    "request_mode",
    "requested_mode",
    "route_mode",
]

SHADOW = "shadow"
CONFIRM = "confirm"
AUTO = "auto"
MODES = (SHADOW, CONFIRM, AUTO)
UNKNOWN_MODE = "unknown_mode"
NO_GTT_DELIVERY = "no_gtt_delivery"
DDPI_NOT_CONFIRMED = "ddpi_not_confirmed"
WEIGHTS_NOT_CONFIRMED = "weights_not_confirmed"
HIGH = "high"  # the confidence label auto places (D132, D149)
NOT_HIGH_CONFIDENCE = "not_high_confidence"  # an auto signal stored without a high label


class ModeRefusedError(HatError):
    """A mode change a gate does not allow; `code` names the gate."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class UnknownModeError(ModeRefusedError):
    """A mode request outside shadow, confirm and auto."""

    def __init__(self) -> None:
        super().__init__(UNKNOWN_MODE)


@dataclass(frozen=True)
class Gates:
    """What confirm and auto need beside a Live sleeve (HAT-EXEC-012, HAT-GOV-085)."""

    account_status: str
    capabilities: Mapping[str, object]
    ddpi_confirmed: bool
    weights_confirmed: bool


def _gtt_ready(account_status: str, capabilities: Mapping[str, object]) -> bool:
    return account_status == ACCOUNT_ACTIVE and capabilities.get("gtt_delivery") is True


def gate_refusal(requested: str, gates: Gates) -> str | None:
    """The first gate that keeps `requested` from running, or None: `unknown_mode` outside the
    three modes; for confirm and auto `no_gtt_delivery` (an inactive account or no GTT for
    delivery), `ddpi_not_confirmed`, then `weights_not_confirmed`. Shadow needs none."""
    if requested not in MODES:
        return UNKNOWN_MODE
    if requested == SHADOW:
        return None
    if not _gtt_ready(gates.account_status, gates.capabilities):
        return NO_GTT_DELIVERY
    if gates.ddpi_confirmed is not True:
        return DDPI_NOT_CONFIRMED
    if gates.weights_confirmed is not True:
        return WEIGHTS_NOT_CONFIRMED
    return None


def _weights_confirmed(session: Session, user_id: uuid.UUID, at: datetime) -> bool:
    """Whether the user's policy in force at `at` confirms the base weights and cluster cap. No
    policy, or a stored one that does not parse, reads as unconfirmed (fail closed)."""
    rows = session.scalars(
        select(HatRiskPolicyVersion).where(HatRiskPolicyVersion.user_id == user_id)
    ).all()
    try:  # read here, not through `policy_service`, which reaches the backtest and the engine
        versions = [
            PolicyVersion(
                r.id, r.version, r.effective_from, r.superseded_at, parse_policy(r.policy)
            )
            for r in rows
        ]
    except PolicyError:
        return False
    version = effective_version(versions, at)
    return version is not None and version.policy.weights_confirmed


def account_gates(session: Session, account: HatBrokerAccount | None, at: datetime) -> Gates:
    """The gates of `account` as they stand for an instant `at`: the stored status and
    capabilities as they are now (losing GTT narrows at once), DDPI confirmed only if it was by
    `at`, and the owner's policy in force at `at`. A missing account passes none."""
    if account is None:
        return Gates("", {}, ddpi_confirmed=False, weights_confirmed=False)
    confirmed_at = account.ddpi_confirmed_at
    ddpi = (
        account.ddpi_confirmed is True
        and confirmed_at is not None
        and as_utc(confirmed_at) <= as_utc(at)
    )
    return Gates(
        account.status,
        account.capabilities or {},
        ddpi_confirmed=ddpi,
        weights_confirmed=_weights_confirmed(session, account.user_id, at),
    )


def real_orders_allowed(
    account_status: str, capabilities: Mapping[str, object], reported: bool
) -> bool:
    """HAT-EXEC-012 at placement: a real entry needs an active account whose stored
    capabilities report GTT for delivery and an adapter that reports it now (`reported`). The
    stored value is a cache of the adapter's (HAT-BRK-092), so it can only narrow: a stored
    `true` never enables a real order the adapter does not report."""
    return _gtt_ready(account_status, capabilities) and reported is True


def effective_mode(requested: str, gates: Gates) -> str:
    """The mode a bot runs in: shadow unless `requested` is one of the three and passes every
    gate (`gate_refusal`)."""
    if requested not in MODES or gate_refusal(requested, gates) is not None:
        return SHADOW
    return requested


def request_mode(requested: str, gates: Gates) -> str:
    """Accept a mode request or refuse it: UnknownModeError for a mode outside the three,
    ModeRefusedError (with the gate's code) when a gate does not pass."""
    refusal = gate_refusal(requested, gates)
    if refusal == UNKNOWN_MODE:
        raise UnknownModeError()
    if refusal is not None:
        raise ModeRefusedError(refusal)
    return requested


def route_mode(mode: str, confidence: str | None) -> str:
    """The route of an entry in effective `mode` (HAT-EXEC-014, D149): auto places only a
    signal labelled high; any other label, or none, goes to the owner as in confirm. Shadow
    and confirm are unchanged."""
    if mode == AUTO and confidence != HIGH:
        return CONFIRM
    return mode


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
    """The bot's queued changes the open did not refuse (a refused one never takes effect)."""
    rows = session.scalars(
        select(HatModeChange).where(
            HatModeChange.bot_id == bot_id, HatModeChange.refused_reason.is_(None)
        )
    )
    return [ModeChange(r.to_mode, r.requested_at, r.effective_from) for r in rows]


def requested_mode(session: Session, bot: HatBot, at: datetime) -> str:
    """The mode the user asked for that is in force at `at`: the latest queued change effective
    by then and not refused at its open, else the bot's stored mode; anything outside the three
    modes reads as shadow."""
    mode = queued_mode(bot.execution_mode, _changes(session, bot.id), at)
    return mode if mode in MODES else SHADOW


def bot_mode(session: Session, bot: HatBot, at: datetime, calendar: SessionCalendar | None) -> str:
    """The effective mode of `bot` for an entry at `at`: shadow unless its sleeve is Live
    (HAT-PORT-063: research, backtested, shadow, probation and retired sleeves, and any unknown
    state, route as shadow whatever mode was asked); else the queued mode in force at the open
    of `at`'s session, gated by the account's gates at that open (a missing account is
    shadow)."""
    if lifecycle.routes_as_shadow(bot.lifecycle_state):
        return SHADOW
    anchor = mode_anchor(calendar, at)
    requested = requested_mode(session, bot, anchor)
    account = session.get(HatBrokerAccount, bot.broker_account_id)
    return effective_mode(requested, account_gates(session, account, anchor))


def pending_change(session: Session, bot: HatBot, at: datetime) -> HatModeChange | None:
    """The bot's latest queued change still waiting for its open at `at`, if any."""
    return session.scalars(
        select(HatModeChange)
        .where(
            HatModeChange.bot_id == bot.id,
            HatModeChange.effective_from > as_utc(at),
            HatModeChange.refused_reason.is_(None),
        )
        .order_by(HatModeChange.effective_from.desc(), HatModeChange.requested_at.desc())
    ).first()


@dataclass(frozen=True)
class ModeView:
    """A bot's mode as `/status` and the dashboard show it: the mode asked for, the one it runs
    in, why they differ (`sleeve_<state>` or the gate's code), and the queued change, if any."""

    asked: str
    effective: str
    why: str | None
    pending: str | None
    pending_from: datetime | None


def mode_view(
    session: Session, bot: HatBot, at: datetime, calendar: SessionCalendar | None
) -> ModeView:
    """The bot's `ModeView` at `at` (the same reads as `bot_mode`)."""
    anchor = mode_anchor(calendar, at)
    asked = requested_mode(session, bot, anchor)
    effective = bot_mode(session, bot, at, calendar)
    why = None
    if effective != asked:
        if lifecycle.routes_as_shadow(bot.lifecycle_state):
            why = f"sleeve_{bot.lifecycle_state}"
        else:
            account = session.get(HatBrokerAccount, bot.broker_account_id)
            why = gate_refusal(asked, account_gates(session, account, anchor))
    queued = pending_change(session, bot, at)
    return ModeView(
        asked,
        effective,
        why,
        queued.to_mode if queued is not None else None,
        as_utc(queued.effective_from) if queued is not None else None,
    )
