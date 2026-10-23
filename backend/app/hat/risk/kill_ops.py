"""Kill switches as database operations: trip, re-arm, the health sync and the state the entry
gate reads (HAT-RISK-060..065). The rules are the pure ones in `kill.py`; the model is
`verification/tla/KillSwitch.tla`.

Every function changes state and appends its event in the caller's open transaction through
`events.record`, so the two commit or roll back together. None commits. Each read takes the
latest row of each (scope, bot, stream) from the database (`read.kill.kill_rows`), so a feed that
flaps for months reads as many rows as it has streams.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from ..engine import journal
from ..engine.lifecycle import AWAITING_APPROVAL
from ..events import record
from ..models import HatApproval, HatBot, HatKillSwitch, HatSignal
from ..read.kill import kill_rows, reasons_of
from ..settings import load_settings
from . import kill
from .controls import ControlError, actor_payload

__all__ = ["cancel_pending_approvals", "kill_state", "rearm_kill", "sync_health", "trip_kill"]

logger = logging.getLogger(__name__)


def _aggregate(scope: str, bot_id: uuid.UUID | None) -> uuid.UUID:
    """The event aggregate of a kill: the bot, or for the account the tenant's id."""
    return bot_id if scope == "bot" and bot_id is not None else load_settings().user_id


def kill_state(session: Session, bot_id: uuid.UUID) -> kill.KillState:
    """The switches bearing on `bot_id` now, for the entry gate."""
    bot = session.get(HatBot, bot_id)
    paused = bot is None or bot.status != "active"  # an unknown or non-active bot takes no entry
    rows = kill_rows(session, keys=(("account", None), ("bot", bot_id)))
    return kill.kill_state_of(rows, bot_id, paused=paused)


def cancel_pending_approvals(
    session: Session, bot_id: uuid.UUID | None, now: datetime, reason: str
) -> int:
    """End every approval that is still undecided (all bots, or one bot's) as `expired` and
    record `signal.expired` through the journal, so it can no longer be taken. A card whose
    signal has already left AwaitingApproval has no step to take (HAT-EXEC-042): it is closed
    without an event, so the log never holds a step the state did not take. Returns how many."""
    query = select(HatApproval, HatSignal).join(HatSignal, HatSignal.id == HatApproval.signal_id)
    query = query.where(HatApproval.decision.is_(None))
    if bot_id is not None:
        query = query.where(HatSignal.bot_id == bot_id)
    count = 0
    for approval, signal in session.execute(query).all():
        approval.decision = "expired"
        approval.decided_at = now
        if signal.state == AWAITING_APPROVAL:
            journal.record(
                session,
                signal,
                journal.on_signal(signal, "signal.expired", {"reason": reason}, now=now),
            )
        else:
            logger.warning("approval of signal %s closed in %s", signal.id, signal.state)
        count += 1
    return count


def trip_kill(
    session: Session,
    *,
    scope: str,
    bot_id: uuid.UUID | None,
    reason: str,
    now: datetime,
    actor: str = "engine",
    chat_id: int | None = None,
) -> HatKillSwitch | None:
    """Trip a kill switch and append `kill.tripped`; None when the key's stream (the risk
    reasons together, each health reason alone) is already tripped (a repeat adds no row). A
    drawdown or manual trip also ends the undecided approvals it covers; a health trip does not
    (the take re-checks the gate)."""
    if reason not in kill.REASONS or scope not in ("account", "bot"):
        raise ControlError("unknown kill scope or reason")
    if (scope == "bot") != (bot_id is not None):
        raise ControlError("a bot kill names its bot and an account kill names none")
    channel = kill.channel_of(reason)
    stream = kill.stream_of(reason)
    if (
        kill.current_state(
            kill_rows(session, keys=((scope, bot_id),)), scope, bot_id, stream=stream
        )
        == kill.TRIPPED
    ):
        return None
    row = HatKillSwitch(
        scope=scope, bot_id=bot_id, state=kill.TRIPPED, tripped_at=now, reason=reason
    )
    session.add(row)
    session.flush()
    record(
        session,
        aggregate_type="bot",
        aggregate_id=_aggregate(scope, bot_id),
        event_type="kill.tripped",
        actor=actor,
        payload=actor_payload(actor, chat_id, {"scope": scope, "reason": reason}),
        bot_id=bot_id,
        occurred_at=now,
    )
    if channel == "risk":
        cancel_pending_approvals(session, bot_id if scope == "bot" else None, now, "kill")
    return row


def _latest_trip(
    session: Session, scope: str, bot_id: uuid.UUID | None, stream: str
) -> HatKillSwitch | None:
    """The latest tripped row of the key's stream, which a re-arm row ends."""
    return session.scalars(
        select(HatKillSwitch)
        .where(
            HatKillSwitch.scope == scope,
            _bot_is(bot_id),
            HatKillSwitch.state == kill.TRIPPED,
            HatKillSwitch.reason.in_(reasons_of(stream)),
        )
        .order_by(HatKillSwitch.tripped_at.desc())
        .limit(1)
    ).first()


def _bot_is(bot_id: uuid.UUID | None) -> ColumnElement[bool]:
    return HatKillSwitch.bot_id.is_(None) if bot_id is None else HatKillSwitch.bot_id == bot_id


def _arm(
    session: Session,
    scope: str,
    bot_id: uuid.UUID | None,
    stream: str,
    now: datetime,
    actor: str,
    chat_id: int | None,
) -> HatKillSwitch | None:
    if (
        kill.current_state(
            kill_rows(session, keys=((scope, bot_id),)), scope, bot_id, stream=stream
        )
        != kill.TRIPPED
    ):
        return None
    trip = _latest_trip(session, scope, bot_id, stream)
    if trip is None:
        return None
    row = HatKillSwitch(
        scope=scope,
        bot_id=bot_id,
        state=kill.ARMED,
        tripped_at=trip.tripped_at,
        reason=trip.reason,
        rearmed_at=now,
    )
    session.add(row)
    session.flush()
    record(
        session,
        aggregate_type="bot",
        aggregate_id=_aggregate(scope, bot_id),
        event_type="kill.rearmed",
        actor=actor,
        payload=actor_payload(actor, chat_id, {"scope": scope, "reason": trip.reason}),
        bot_id=bot_id,
        occurred_at=now,
    )
    return row


def rearm_kill(
    session: Session,
    *,
    scope: str,
    bot_id: uuid.UUID | None,
    confirmed: bool,
    now: datetime,
    chat_id: int,
    actor: str = "user",
) -> HatKillSwitch | None:
    """Re-arm a drawdown or manual kill: only with a second confirmation, only by a user, and it
    appends `kill.rearmed`. None when there is nothing tripped to re-arm. A health kill is not
    re-armed here: it clears itself (`sync_health`)."""
    if actor != "user":
        raise ControlError("a drawdown or manual kill is re-armed by a user only")
    if confirmed is not True:
        raise ControlError("re-arming needs a second confirmation")
    return _arm(session, scope, bot_id, "risk", now, actor, chat_id)


def sync_health(
    session: Session,
    *,
    scope: str,
    bot_id: uuid.UUID | None,
    failing: bool,
    reason: str,
    now: datetime,
) -> HatKillSwitch | None:
    """One health check's result for a bot (or the account): trip its reason while it fails,
    clear that reason by itself the moment it passes (HAT-RISK-063). A check clears only its own
    reason: the feed recovering (`feed_health`) leaves a failed quality gate (`data_health`)
    tripped until the next gate passes (HAT-DATA-065)."""
    if reason not in kill.HEALTH_REASONS:
        raise ControlError("a health check names data_health, feed_health or broker_health")
    if failing:
        return trip_kill(session, scope=scope, bot_id=bot_id, reason=reason, now=now)
    return _arm(session, scope, bot_id, reason, now, "engine", None)
