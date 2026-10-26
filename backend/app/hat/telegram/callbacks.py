"""Button presses: approval cards and second confirmations (HAT-TG-033, HAT-TG-035,
HAT-TG-040..044, HAT-GOV-082).

**Take and Skip.** A button is checked at this edge before anything else runs: its MAC must
verify under `HAT_APPROVAL_SECRET`, the approval of its signal must exist, the expiry it names
must be the approval's, and the approval's stored `token_hash` must be the hash of the card
token the signer issues for it (`ApprovalSigner.matches`). A decided approval is refused here,
and again under the bot's lock by `approvals.take` / `skip`, so a token is used once and a card
is decided once (HAT-TG-041). A bad, used or forged token is refused without touching the signal.
Only then does the `ApprovalDesk` (the execution router of the bot's account) take or skip:
the Take reads the live price, `locked_upper_circuit` and the placement conditions at the tap
and fails closed when any is unknown (HAT-TG-043).

**Confirmations.** A confirmed `/flatten` sells every open real position through the exit path
(`TradeManager.flatten`: purpose flatten, side sell, qty_open) and records one
`flatten.executed` with the chat id as a discipline breach; a confirmed `/arm` re-arms the
tripped drawdown and manual kills; a confirmed `/mode … auto` queues auto for the next session.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..configs.records import Bot
from ..configs.unit import configs_of
from ..engine import approvals, lifecycle
from ..engine.approvals import ApprovalDecision, Placement
from ..events import record
from ..execution.manage import FlattenReport, TradeManager
from ..execution.order_book import ENTRY
from ..execution.router import ExecutionRouter, PlacementCheck
from ..governance import Mode
from ..models import HatApproval, HatOrder, HatSignal
from ..risk import kill_ops
from ..settings import load_settings
from .commands import Reply, queue_modes, rearm, refused
from .confirm import PendingConfirmations
from .tokens import ACTIONS, CONFIRM, TAKE, ApprovalSigner

__all__ = [
    "ApprovalDesk",
    "ButtonContext",
    "Flattener",
    "ManagerFlattener",
    "RouterDesk",
    "TakeInputs",
    "run_callback",
    "take_reply",
]

logger = logging.getLogger(__name__)

EVENT_FLATTEN = "flatten.executed"
_SKIPPED = "skipped"
_ENTRY_NOT_SENT = ("cancelled", "rejected", "expired")


class ApprovalDesk(Protocol):
    def take(
        self, session: Session, signal_id: uuid.UUID, *, now: datetime, chat_id: int
    ) -> ApprovalDecision: ...

    def skip(
        self, session: Session, signal_id: uuid.UUID, *, now: datetime, chat_id: int
    ) -> ApprovalDecision: ...


# The live inputs of a Take read at the tap: (price, locked at upper circuit, placement check).
TakeInputs = Callable[
    [Session, HatSignal, datetime], tuple[Decimal | None, bool | None, PlacementCheck]
]


class RouterDesk:
    """Takes and skips through the execution router of the signal's broker account."""

    def __init__(
        self, router_for: Callable[[uuid.UUID], ExecutionRouter | None], inputs: TakeInputs
    ) -> None:
        self._router_for = router_for
        self._inputs = inputs

    def _router(self, session: Session, signal_id: uuid.UUID) -> ExecutionRouter | None:
        signal = session.get(HatSignal, signal_id)
        bot = configs_of(session).get(Bot, signal.bot_id) if signal is not None else None
        return self._router_for(bot.broker_account_id) if bot is not None else None

    def take(
        self, session: Session, signal_id: uuid.UUID, *, now: datetime, chat_id: int
    ) -> ApprovalDecision:
        router = self._router(session, signal_id)
        signal = session.get(HatSignal, signal_id)
        if router is None or signal is None:
            return ApprovalDecision(signal_id, "no_router")
        price, locked, check = self._inputs(session, signal, now)
        try:
            decision = router.take(
                session,
                signal_id,
                price=price,
                locked_upper_circuit=locked,
                now=now,
                chat_id=chat_id,
                check=check,
            )
        except Exception:  # the Take may have committed: what happens next is read, not guessed
            session.rollback()
            logger.exception("the Take of signal %s raised", signal_id)
            return _after_raise(session, signal_id)
        return decision

    def skip(
        self, session: Session, signal_id: uuid.UUID, *, now: datetime, chat_id: int
    ) -> ApprovalDecision:
        router = self._router(session, signal_id)
        if router is None:
            return ApprovalDecision(signal_id, "no_router")
        return router.skip(session, signal_id, now=now, chat_id=chat_id)


class Flattener(Protocol):
    def orders_open(self, now: datetime) -> bool: ...

    def flatten(self, session: Session, now: datetime) -> FlattenReport: ...


class ManagerFlattener:
    """Flattens through the trade manager of every broker account: the same `TradeManager`
    instances the live exits use, so their sales of a position are serialised."""

    def __init__(self, managers: Callable[[], Sequence[tuple[uuid.UUID, TradeManager]]]) -> None:
        self._managers = managers

    def orders_open(self, now: datetime) -> bool:
        """Some account can sell now (inside the session, before continuous trading ends at
        15:15)."""
        return any(manager.orders_allowed(now) for _account, manager in self._managers())

    def flatten(self, session: Session, now: datetime) -> FlattenReport:
        total = FlattenReport()
        for account_id, manager in self._managers():
            try:
                report = manager.flatten(session, account_id, now)
            except Exception:  # one account's failure must not stop the others being flattened
                session.rollback()
                logger.exception("flatten of account %s failed", account_id)
                total.failed += 1
                continue
            total.positions += report.positions
            total.sent += report.sent
            total.failed += report.failed
        return total


@dataclass(frozen=True)
class ButtonContext:
    chat_id: int
    now: datetime
    signer: ApprovalSigner
    confirmations: PendingConfirmations
    desk: ApprovalDesk | None = None
    flattener: Flattener | None = None


def _approval_button(ctx: ButtonContext, session: Session, data: str) -> tuple[str, str, Reply]:
    command = "take" if data.startswith(TAKE) else "skip"
    claim = ctx.signer.verify(data)
    if claim is None:
        return command, "", refused("bad_token")
    approval = session.get(HatApproval, claim.signal_id)
    if approval is None or not ctx.signer.matches(claim, approval.expires_at, approval.token_hash):
        return command, "", refused("bad_token")
    args = str(claim.signal_id)
    if approval.decision is not None:
        return command, args, refused("already_decided")
    if ctx.desk is None:
        return command, args, refused("no_router")
    if claim.action == TAKE:
        decision = ctx.desk.take(session, claim.signal_id, now=ctx.now, chat_id=ctx.chat_id)
        if decision.outcome == approvals.APPROVED and decision.placement is None:
            sent = _entry_sent(session, claim.signal_id)
            decision = replace(decision, placement=Placement.SENT if sent else Placement.REFUSED)
        return command, args, take_reply(decision)
    decision = ctx.desk.skip(session, claim.signal_id, now=ctx.now, chat_id=ctx.chat_id)
    if decision.outcome != _SKIPPED:
        return command, args, refused(decision.outcome)
    return command, args, Reply(True, text="Skip: skipped")


_TAKE_TEXT = {
    Placement.SENT: "Take: approved, entry sent",
    # refused after the Take (an entry.not_placed alert says why): never reads as placed
    Placement.REFUSED: "Take: approved, entry NOT placed (see alert)",
    Placement.RETRYING: "Take: approved, placement retrying (see alerts)",
}


def take_reply(decision: ApprovalDecision) -> Reply:
    """The answer to a Take: what will happen to its entry, never more than is known."""
    if decision.placement is Placement.UNKNOWN:
        # the Take may or may not have been recorded; the card keeps its buttons
        return refused("unknown", "Take: outcome unknown, see alerts before tapping again")
    if decision.outcome != approvals.APPROVED:
        return refused(decision.outcome)
    return Reply(True, text=_TAKE_TEXT[decision.placement or Placement.REFUSED])


def _after_raise(session: Session, signal_id: uuid.UUID) -> ApprovalDecision:
    """A Take whose router call raised, read back: not recorded (an `error` refusal, the card
    stays), recorded and left for `resume_entries` (retrying: the router remembered it
    stranded), its entry sent, or refused after the Take."""
    try:
        session.expire_all()
        approval = session.get(HatApproval, signal_id)
        signal = session.get(HatSignal, signal_id)
        if approval is None or signal is None or approval.decision != approvals.TAKE:
            return ApprovalDecision(signal_id, "error")
        if _entry_attempted(session, signal_id):
            placement = Placement.SENT  # it reached the broker, or may have: never sent again
        elif signal.state == lifecycle.AWAITING_APPROVAL:
            placement = Placement.RETRYING  # a reserved entry never attempted: resume sends it
        else:
            placement = Placement.REFUSED
        return ApprovalDecision(signal_id, approvals.APPROVED, placement=placement)
    except Exception:
        session.rollback()
        logger.exception("reading back the Take of signal %s failed", signal_id)
        return ApprovalDecision(signal_id, "unknown", placement=Placement.UNKNOWN)


def _entry_sent(session: Session, signal_id: uuid.UUID) -> bool:
    """The signal has an entry order that reached, or may have reached, the broker."""
    session.expire_all()
    found = session.scalar(
        select(HatOrder.id).where(
            HatOrder.signal_id == signal_id,
            HatOrder.purpose == ENTRY,
            HatOrder.status.not_in(_ENTRY_NOT_SENT),
        )
    )
    return found is not None


def _entry_attempted(session: Session, signal_id: uuid.UUID) -> bool:
    """The signal has an entry order past `pending`, or a `pending` one whose send was stamped
    (`placed_at`): the broker may hold it, so a resume only looks it up by tag (HAT-BRK-102)."""
    found = session.scalar(
        select(HatOrder.id).where(
            HatOrder.signal_id == signal_id,
            HatOrder.purpose == ENTRY,
            HatOrder.status.not_in(_ENTRY_NOT_SENT),
            (HatOrder.status != "pending") | HatOrder.placed_at.is_not(None),
        )
    )
    return found is not None


_KILLED = "\nKill switch tripped: every bot is manage-only until /arm"


def _flatten(ctx: ButtonContext, session: Session) -> Reply:
    if ctx.flattener is None:
        return refused("no_flattener")
    # The owner asked for no exposure: the account kill trips and commits first, market open or
    # not (every bot manage-only, pending cards ended), and the flatten withdraws the working
    # entries; the account stays killed until /arm.
    kill_ops.trip_kill(
        session,
        scope="account",
        bot_id=None,
        reason="manual",
        now=ctx.now,
        actor="user",
        chat_id=ctx.chat_id,
    )
    session.commit()
    if not ctx.flattener.orders_open(ctx.now):
        # nothing can be sold now and nothing is queued: no breach is recorded, and the owner
        # sends /flatten again in the session
        return refused("market_closed", f"Refused: market_closed, nothing sold.{_KILLED}")
    report = ctx.flattener.flatten(session, ctx.now)
    record(
        session,
        aggregate_type="bot",
        aggregate_id=load_settings().user_id,
        event_type=EVENT_FLATTEN,
        actor="user",
        payload={
            "chat_id": ctx.chat_id,
            "positions": report.positions,
            "sent": report.sent,
            "failed": report.failed,
        },
        occurred_at=ctx.now,
    )
    text = f"Flatten: {report.sent} of {report.positions} position(s) sent"
    if report.failed:
        text += f"; {report.failed} failed, see the alerts"
    text += _KILLED
    return Reply(True, text=text)


def _confirmed(ctx: ButtonContext, session: Session, data: str) -> tuple[str, str, Reply]:
    pending, reason = ctx.confirmations.take(data, ctx.chat_id, ctx.now)
    if pending is None:
        return "confirm", "", refused(reason)
    if pending.action == "flatten":
        return "confirm", "flatten", _flatten(ctx, session)
    if pending.action == "arm":
        count, refusal = rearm(session, pending, ctx.now, ctx.chat_id)
        if refusal is not None:
            return "confirm", "arm", refused(refusal)
        return "confirm", "arm", Reply(True, text=f"Re-armed {count} kill switch(es)")
    ids = {uuid.UUID(b) for b in pending.bots}
    bots = configs_of(session).all(Bot, lambda b: b.id in ids)
    if not bots:
        return "confirm", "mode_auto", refused("unknown_bot")
    return (
        "confirm",
        "mode_auto",
        queue_modes(session, bots, Mode.AUTO, ctx.now, ctx.chat_id, confirmed=True),
    )


def run_callback(ctx: ButtonContext, session: Session, data: str) -> tuple[str, str | None, Reply]:
    """Run one button press: (logged command, logged args, reply). The raw token is never
    returned for logging: the args are the signal id or the confirmed action."""
    if data.startswith(CONFIRM):
        command, args, reply = _confirmed(ctx, session, data)
    elif data[:1] in ACTIONS:
        command, args, reply = _approval_button(ctx, session, data)
    else:
        return "callback", None, refused("unknown_callback")
    return command, args or None, reply
