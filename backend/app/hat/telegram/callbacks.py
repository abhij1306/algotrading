"""Button presses: approval cards and second confirmations (08 §3, §4; HAT-TG-033, HAT-TG-035,
HAT-TG-040..044, HAT-GOV-082).

**Take and Skip.** A button is checked at this edge before anything else runs: its MAC must
verify under `HAT_APPROVAL_SECRET`, the approval of its signal must exist, the expiry it names
must be the approval's, and the approval's stored `token_hash` must be the hash of the card
token the signer issues for it (`ApprovalSigner.matches`). A decided approval is refused here,
and again under the bot's lock by `approvals.take` / `skip`, so a token is used once and a card
is decided once (HAT-TG-041). A bad, used or forged token is refused without touching the signal.
Only then does the `ApprovalDesk` (the execution router of the bot's account) take or skip:
the Take reads the live price, `locked_upper_circuit` and the placement conditions at the tap
and fails closed when any is unknown (HAT-TG-043, 07 §3).

**Confirmations.** A confirmed `/flatten` sells every open real position through the exit path
(`TradeManager.flatten`: purpose flatten, side sell, qty_open) and records one
`flatten.executed` with the chat id as a discipline breach; a confirmed `/arm` re-arms the
tripped drawdown and manual kills; a confirmed `/mode … auto` queues auto for the next session.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..engine import approvals
from ..engine.approvals import ApprovalDecision
from ..events import record
from ..execution.manage import FlattenReport, TradeManager
from ..execution.router import ExecutionRouter, PlacementCheck
from ..execution.submit import ENTRY
from ..models import HatApproval, HatBot, HatOrder, HatSignal
from ..settings import load_settings
from .commands import Reply, queue_modes, rearm_all, refused
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
]

logger = logging.getLogger(__name__)

EVENT_FLATTEN = "flatten.executed"
_PLACED_OR_SKIPPED = (approvals.APPROVED, "skipped")
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
        bot = session.get(HatBot, signal.bot_id) if signal is not None else None
        return self._router_for(bot.broker_account_id) if bot is not None else None

    def take(
        self, session: Session, signal_id: uuid.UUID, *, now: datetime, chat_id: int
    ) -> ApprovalDecision:
        router = self._router(session, signal_id)
        signal = session.get(HatSignal, signal_id)
        if router is None or signal is None:
            return ApprovalDecision(signal_id, "no_router")
        price, locked, check = self._inputs(session, signal, now)
        return router.take(
            session,
            signal_id,
            price=price,
            locked_upper_circuit=locked,
            now=now,
            chat_id=chat_id,
            check=check,
        )

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
        """Some account can sell now (inside the session, before 15:30)."""
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
    else:
        decision = ctx.desk.skip(session, claim.signal_id, now=ctx.now, chat_id=ctx.chat_id)
    if decision.outcome not in _PLACED_OR_SKIPPED:
        return command, args, refused(decision.outcome)
    if claim.action == TAKE and not _entry_sent(session, claim.signal_id):
        # the Take is recorded, but the placement was refused after it (an entry.not_placed
        # alert says why): the reply must not read as a placed order
        return command, args, Reply(True, text="Take: approved, entry NOT placed (see alert)")
    text = "Take: approved, entry sent" if claim.action == TAKE else "Skip: skipped"
    return command, args, Reply(True, text=text)


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


def _flatten(ctx: ButtonContext, session: Session) -> Reply:
    if ctx.flattener is None:
        return refused("no_flattener")
    if not ctx.flattener.orders_open(ctx.now):
        # nothing could be sold: no breach is recorded and the owner sends /flatten again
        return refused("market_closed")
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
    return Reply(True, text=text)


def _confirmed(ctx: ButtonContext, session: Session, data: str) -> tuple[str, str, Reply]:
    pending, reason = ctx.confirmations.take(data, ctx.chat_id, ctx.now)
    if pending is None:
        return "confirm", "", refused(reason)
    if pending.action == "flatten":
        return "confirm", "flatten", _flatten(ctx, session)
    if pending.action == "arm":
        count = rearm_all(session, ctx.now, ctx.chat_id)
        if not count:
            return "confirm", "arm", refused("nothing_tripped")
        return "confirm", "arm", Reply(True, text=f"Re-armed {count} kill switch(es)")
    ids = {uuid.UUID(b) for b in pending.bots}
    bots = list(session.scalars(select(HatBot).where(HatBot.id.in_(ids)).order_by(HatBot.id)))
    if not bots:
        return "confirm", "mode_auto", refused("unknown_bot")
    return "confirm", "mode_auto", queue_modes(session, bots, "auto", ctx.now, ctx.chat_id)


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
