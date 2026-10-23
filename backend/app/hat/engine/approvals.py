"""Confirm-mode approvals: Take, Skip, expiry and the veto score (HAT-EXEC-030..032,
HAT-EXEC-130, HAT-TG-042, HAT-TG-043, HAT-TG-045).

An approval card is written with its signal (`trades.open_entry` in confirm mode) and expires
`APPROVAL_WINDOW` (5 minutes) after it is sent. A decision is taken once, under the bot's lock:

- **Take** before `expires_at` re-checks the price against the bot's max-chase percentage, the
  one fixed at the trigger, through the same `max_chase` criterion the entry filter ran
  (`LiveContext.for_entry`, HAT-CRIT-122). A pass records `signal.approved` and the router places
  the real order; a fail records the decision with its price and ends the signal Rejected
  (`max_chase`), and no order is placed (HAT-EXEC-031, HAT-TG-043).
- **Skip** before `expires_at` ends the signal Cancelled (`signal.skipped`).
- An approval still undecided at `expires_at` ends Expired (`signal.expired`); a Take or Skip at
  or after `expires_at` is refused and expires it (HAT-EXEC-030).

A skipped or expired signal continues as a virtual veto trade (`trades.open_veto_trade`,
HAT-EXEC-032); `veto_score` sums the R those trades avoided and missed (HAT-EXEC-130), and
after `VERDICT_DECISIONS` decisions `veto_verdict` says whether the skips beat the takes
(HAT-EXEC-131).

Nothing here calls a broker: the Take returns whether the router may place the order.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import HatApproval, HatEvent, HatPosition, HatSignal, HatTradeResult
from ..money import exact
from ..risk.modes import Mode
from ..time import as_utc
from . import journal, lifecycle, trades
from .criteria.base import CriterionResult
from .criteria.live import LiveContext, MaxChaseParams, max_chase

__all__ = [
    "APPROVED",
    "ApprovalDecision",
    "VERDICT_DECISIONS",
    "VetoScore",
    "VetoVerdict",
    "chase_recheck",
    "expire_due",
    "open_missing_veto_trades",
    "skip",
    "skipped_results",
    "take",
    "veto_history",
    "veto_score",
    "veto_verdict",
]

APPROVED = "approved"
TAKE = "take"
SKIP = "skip"
EXPIRED = "expired"
_DECIDED_AWAY = (SKIP, EXPIRED)
_ZERO = Decimal(0)


@dataclass(frozen=True)
class ApprovalDecision:
    """What a Take or Skip did. `place` is True only for a Take that passed the re-check: the
    router then places the real order. `outcome` is a fixed word: approved, max_chase, skipped,
    expired, already_decided, not_awaiting, unknown_signal, or the risk reason code of a live
    condition that refused the Take."""

    signal_id: uuid.UUID
    outcome: str
    place: bool = False
    placement: Placement | None = None  # an approved Take's entry, once the desk knows it


class Placement(StrEnum):
    """What became of an approved Take's entry: sent to the broker (or may have been), refused
    after the Take, retrying (its placement raised and `resume_entries` places it), or unknown
    (the raise left the Take itself unreadable)."""

    SENT = "sent"
    REFUSED = "refused"
    RETRYING = "retrying"
    UNKNOWN = "unknown"


def chase_recheck(
    price: Decimal | None,
    trigger_level: Decimal | None,
    pct: Decimal | None,
    locked_upper_circuit: bool | None,
    at: datetime,
) -> CriterionResult:
    """The Take re-check (HAT-EXEC-031): the `max_chase` entry filter on the decision price, the
    trigger level and the percentage fixed at the trigger. A missing input fails closed."""
    if pct is None or not pct.is_finite() or pct <= 0 or trigger_level is None:
        return CriterionResult(False, 0.0, None, "no_data")
    ctx = LiveContext(at=at, price=price).for_entry(
        entry=price if price is not None else _ZERO,
        stop=None,
        adtr20=None,
        trigger_level=trigger_level,
        locked_upper_circuit=locked_upper_circuit,
    )
    return max_chase(ctx, MaxChaseParams(pct=float(pct)))


def _triggered_pct(session: Session, signal_id: uuid.UUID) -> Decimal | None:
    payload = session.scalar(
        select(HatEvent.payload).where(
            HatEvent.correlation_id == signal_id, HatEvent.event_type == "signal.triggered"
        )
    )
    raw = (payload or {}).get("max_chase_pct")
    return exact(raw, floats=True)


def _locked(session: Session, signal_id: uuid.UUID) -> tuple[HatSignal, HatApproval] | None:
    signal = session.get(HatSignal, signal_id)
    if signal is None:
        return None
    trades.lock_bot(session, signal.bot_id)
    session.refresh(signal)
    approval = session.get(HatApproval, signal_id)
    if approval is None:
        return None
    session.refresh(approval)
    return signal, approval


def _signal_event(
    session: Session,
    signal: HatSignal,
    event_type: str,
    payload: dict[str, object],
    now: datetime,
    *,
    chat_id: int | None = None,
) -> None:
    """Record a decision event and move the signal along it, in the caller's transaction (the
    journal refuses a step the table forbids)."""
    data = {**payload, "chat_id": chat_id} if chat_id is not None else payload
    actor = "user" if chat_id is not None else "engine"
    journal.record(
        session, signal, journal.on_signal(signal, event_type, data, now=now, actor=actor)
    )


def _expire(session: Session, signal: HatSignal, approval: HatApproval, now: datetime) -> None:
    approval.decision = EXPIRED
    approval.decided_at = as_utc(now)
    _signal_event(session, signal, "signal.expired", {"reason": "approval_window"}, now)


def _open_decision(
    session: Session, signal_id: uuid.UUID, now: datetime
) -> tuple[HatSignal, HatApproval] | ApprovalDecision:
    """The locked signal and its undecided approval, or the refusal: an approval at or past its
    expiry is expired here and refused (HAT-EXEC-030)."""
    found = _locked(session, signal_id)
    if found is None:
        return ApprovalDecision(signal_id, "unknown_signal")
    signal, approval = found
    if approval.decision is not None:
        return ApprovalDecision(signal_id, "already_decided")
    if signal.state != lifecycle.AWAITING_APPROVAL:
        return ApprovalDecision(signal_id, "not_awaiting")
    if as_utc(now) >= as_utc(approval.expires_at):
        _expire(session, signal, approval, now)
        trades.open_veto_trade(session, signal.id, now)
        return ApprovalDecision(signal_id, EXPIRED)
    return signal, approval


def take(
    session: Session,
    signal_id: uuid.UUID,
    *,
    price: Decimal | None,
    locked_upper_circuit: bool | None,
    now: datetime,
    chat_id: int,
    refusal: str | None = None,
) -> ApprovalDecision:
    """The user's Take (in the caller's transaction, no commit). The decision and its price are
    recorded either way; only a Take before expiry, with no `refusal` from the live conditions
    (market hours, feed, quality gate, regime gate; the router reads them), whose price passes
    the max-chase re-check returns `place=True`. A refused Take ends the signal Rejected with
    the refusal's reason."""
    opened = _open_decision(session, signal_id, now)
    if isinstance(opened, ApprovalDecision):
        return opened
    signal, approval = opened
    approval.decision = TAKE
    approval.decided_at = as_utc(now)
    approval.price_at_decision = price
    if refusal is not None:
        _signal_event(
            session,
            signal,
            "signal.rejected",
            {"reason": refusal, "stage": "approval"},
            now,
            chat_id=chat_id,
        )
        return ApprovalDecision(signal_id, refusal)
    check = chase_recheck(
        price,
        Decimal(signal.trigger_price),
        _triggered_pct(session, signal.id),
        locked_upper_circuit,
        now,
    )
    if not check.passed:
        _signal_event(
            session,
            signal,
            "signal.rejected",
            {"reason": "max_chase", "stage": "approval"},
            now,
            chat_id=chat_id,
        )
        return ApprovalDecision(signal_id, "max_chase")
    _signal_event(session, signal, "signal.approved", {"price": str(price)}, now, chat_id=chat_id)
    return ApprovalDecision(signal_id, APPROVED, place=True)


def skip(
    session: Session, signal_id: uuid.UUID, *, now: datetime, chat_id: int
) -> ApprovalDecision:
    """The user's Skip before expiry: the signal ends Cancelled and continues as a veto trade."""
    opened = _open_decision(session, signal_id, now)
    if isinstance(opened, ApprovalDecision):
        return opened
    signal, approval = opened
    approval.decision = SKIP
    approval.decided_at = as_utc(now)
    _signal_event(session, signal, "signal.skipped", {"reason": "skipped"}, now, chat_id=chat_id)
    trades.open_veto_trade(session, signal.id, now)
    return ApprovalDecision(signal_id, "skipped")


def expire_due(session: Session, now: datetime) -> int:
    """Expire every approval still undecided at `now` >= its `expires_at` (`signal.expired`), in
    the caller's transaction, then open the veto trade of every skipped or expired approval that
    has none. Idempotent. Returns how many approvals expired."""
    due = session.scalars(
        select(HatApproval.signal_id).where(
            HatApproval.decision.is_(None), HatApproval.expires_at <= as_utc(now)
        )
    ).all()
    count = 0
    for signal_id in due:
        found = _locked(session, signal_id)
        if found is None:
            continue
        signal, approval = found
        if approval.decision is not None or signal.state != lifecycle.AWAITING_APPROVAL:
            continue
        _expire(session, signal, approval, now)
        count += 1
    open_missing_veto_trades(session, now)
    return count


def open_missing_veto_trades(session: Session, now: datetime) -> int:
    """Open the veto trade of every skipped or expired approval that has none yet (an approval a
    kill ended among them). Returns how many were opened."""
    pending = session.scalars(
        select(HatApproval.signal_id)
        .join(HatSignal, HatSignal.id == HatApproval.signal_id)
        .outerjoin(
            HatPosition,
            (HatPosition.signal_id == HatApproval.signal_id) & HatPosition.is_virtual.is_(True),
        )
        .where(HatApproval.decision.in_(_DECIDED_AWAY), HatPosition.id.is_(None))
    ).all()
    opened = 0
    for signal_id in pending:
        if trades.open_veto_trade(session, signal_id, now) is not None:
            opened += 1
    return opened


# --- the veto score (HAT-EXEC-130) -------------------------------------------------------------


@dataclass(frozen=True)
class VetoScore:
    """The R the skips avoided (losses not taken, as a positive number) and missed (gains not
    taken), and how many closed veto trades they sum."""

    avoided_r: Decimal
    missed_r: Decimal
    trades: int


def veto_score(results: Iterable[Decimal]) -> VetoScore:
    """avoided = -sum(r for r < 0), missed = sum(r for r > 0), over the R multiples of the closed
    veto trades (HAT-EXEC-130). Exact: the R values are Decimal."""
    avoided = _ZERO
    missed = _ZERO
    count = 0
    for r in results:
        count += 1
        if r < 0:
            avoided -= r
        elif r > 0:
            missed += r
    return VetoScore(avoided, missed, count)


def skipped_results(session: Session, start: datetime, end: datetime) -> list[Decimal]:
    """The R multiples of the veto trades of skipped or expired approvals that closed in
    [start, end): the inputs of the week's veto score."""
    rows = session.scalars(
        select(HatTradeResult.r_multiple)
        .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
        .join(HatSignal, HatSignal.id == HatPosition.signal_id)
        .join(HatApproval, HatApproval.signal_id == HatSignal.id)
        .where(
            HatPosition.is_virtual.is_(True),
            HatSignal.mode != Mode.SHADOW,
            HatApproval.decision.in_(_DECIDED_AWAY),
            HatPosition.closed_at >= as_utc(start),
            HatPosition.closed_at < as_utc(end),
        )
    ).all()
    return [Decimal(r) for r in rows]


# --- the veto verdict (HAT-EXEC-131) -----------------------------------------------------------

VERDICT_DECISIONS = 50
SKIPS_BEAT_TAKES = "skips beat takes"
SKIPS_DO_NOT_BEAT_TAKES = "skips do not beat takes"
INSUFFICIENT_DATA = "insufficient data"


@dataclass(frozen=True)
class VetoVerdict:
    """After `VERDICT_DECISIONS` takes and skips: the mean R of the skipped trades (their veto
    trades) and of the taken ones (their real trades), None when a group has no closed trade.
    The skips beat the takes when the skipped trades did worse than the taken ones."""

    decisions: int
    skipped_mean: Decimal | None
    taken_mean: Decimal | None
    verdict: str


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, _ZERO) / len(values) if values else None


def veto_verdict(
    decisions: int, skipped: Sequence[Decimal], taken: Sequence[Decimal]
) -> VetoVerdict | None:
    """None before `VERDICT_DECISIONS` decisions; else the means and the verdict. A group with
    no closed trade has no mean and the verdict is insufficient data (never a division by
    zero)."""
    if decisions < VERDICT_DECISIONS:
        return None
    skipped_mean, taken_mean = _mean(skipped), _mean(taken)
    if skipped_mean is None or taken_mean is None:
        verdict = INSUFFICIENT_DATA
    # compared exactly: sum(s) / |s| < sum(t) / |t|, without the rounding of either mean
    elif sum(skipped, _ZERO) * len(taken) < sum(taken, _ZERO) * len(skipped):
        verdict = SKIPS_BEAT_TAKES
    else:
        verdict = SKIPS_DO_NOT_BEAT_TAKES
    return VetoVerdict(decisions, skipped_mean, taken_mean, verdict)


def _closed_r(session: Session, decision: str, *, virtual: bool) -> list[Decimal]:
    rows = session.scalars(
        select(HatTradeResult.r_multiple)
        .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
        .join(HatApproval, HatApproval.signal_id == HatPosition.signal_id)
        .where(HatApproval.decision == decision, HatPosition.is_virtual.is_(virtual))
    ).all()
    return [Decimal(r) for r in rows]


def veto_history(session: Session) -> tuple[int, list[Decimal], list[Decimal]]:
    """The inputs of `veto_verdict`: the number of takes and skips so far, the R of the closed
    veto trades of the skips, and the R of the closed real trades of the takes."""
    decisions = session.scalar(
        select(func.count()).select_from(HatApproval).where(HatApproval.decision.in_((TAKE, SKIP)))
    )
    return (
        int(decisions or 0),
        _closed_r(session, SKIP, virtual=True),
        _closed_r(session, TAKE, virtual=False),
    )
