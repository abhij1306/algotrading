"""Entries against the database: per-bot serialisation, the entry decision and its signal row,
routing by mode and the approval card (HAT-EXEC-020..031, HAT-EXEC-040, -043). The reads an
entry decision takes are `snapshot`'s; a shadow entry's virtual position is `shadow`'s.

Every function works in the caller's open transaction and never commits. A state change and the
event that explains it are written together, so a replay of the events always reproduces the
stored state (`journal.replay_state`).

**One locked transaction per bot.** `evaluate_signal` is pure over the snapshot it is given, so
two evaluations that read the same snapshot could both take the last position slot. `open_entry`
therefore takes the bot's lock first, reads the snapshot, decides and inserts under it; a second
entry for the same bot waits for the first to commit and then sees its position. On Postgres the
lock is a transaction-scoped advisory lock keyed by the bot; SQLite has one writer at a time and
needs none.

**One entry decision at a time per owner (HAT-RISK-030).** The global open-risk cap sums every
bot of the owner, the same bots whose allocations make up total equity, so `open_entry` hands
the decision the slots of all of them (`owner_slots`; the per-bot caps filter them to the bot's
own) and, after the bot's lock, takes the owner's lock, so two bots' triggers in one instant
never both fit on one snapshot. Only an entry adds open risk; the other writers of a slot (a
fill, a Take, a skip, an expiry, a stop raise, an exit) run under the bot's lock alone and can
only keep or shrink it, so an entry that reads before one of them commits over-counts, never
under-counts.

**Three modes, no broker (D20, HAT-EXEC-020).** `open_entry` takes the effective mode the
execution router worked out (`execution/modes.py:bot_mode`) and writes what that mode needs under
the bot's lock: shadow opens the virtual position at once; confirm stores the signal in
AwaitingApproval with its `hat_approval` card (expiring 5 minutes later, HAT-EXEC-030); auto
leaves the accepted signal in Triggered, holding its slot, for the router to place the real order
(`execution/router.py`). Nothing in this module reaches a broker, and it imports none (CONF
`import_graph_boundaries`).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..backtest.evidence import signal_confidence
from ..execution.modes import route_mode
from ..governance import MODE_VALUES, Mode
from ..ids import uuid7
from ..models import HatApproval, HatBot, HatSignal
from ..risk.caps import effective_max_chase_pct
from ..risk.decision import REASON_CODES, RiskDecision, SignalContext
from ..risk.policy import PolicyVersion, effective_version
from ..risk.service import evaluate_signal
from ..time import as_utc
from . import journal, lifecycle, rescale
from .botlock import lock_bot, lock_owner
from .confidence import LABELS, UNKNOWN_LABEL, Confidence
from .positions import Trade, TradeError
from .shadow import SHADOW_FILLS, FillModel, open_virtual
from .snapshot import day_counts, last_hard_stop, owner_slots
from .stops import SKIP_BAD_INPUT, SKIP_NOT_BELOW_ENTRY, SKIP_TOO_NARROW, SKIP_TOO_WIDE
from .watchlist import bot_universe_members

__all__ = [
    "APPROVAL_WINDOW",
    "DECIDE",
    "ENTRY_SKIP_REASONS",
    "EntryOutcome",
    "EntryRequest",
    "TokenIssuer",
    "entry_confidence",
    "open_entry",
    "reject_signal",
    "send_approval",
]

DECIDE = Callable[[SignalContext], RiskDecision]
# HAT-EXEC-030: an approval card expires 5 minutes after it is sent.
APPROVAL_WINDOW = timedelta(minutes=5)
# Issues an approval card's token from (signal id, expires_at); `telegram.tokens.ApprovalSigner`.
TokenIssuer = Callable[[uuid.UUID, datetime], str]
# Why a trigger was skipped before the risk check: the initial stop could not be set, or an entry
# filter failed (HAT-EXEC-122). They are stored as the signal's REJECT reason, like a risk reason.
ENTRY_SKIP_REASONS = frozenset(
    {
        SKIP_BAD_INPUT,
        SKIP_NOT_BELOW_ENTRY,
        SKIP_TOO_WIDE,
        SKIP_TOO_NARROW,
        "adtr_filter",
        "max_chase",
        "level_undefined",
        "no_live_price",
        rescale.SKIP_ACTION_PENDING,
    }
)
_ZERO = Decimal(0)
SIGNAL_TRIGGERED = journal.SIGNAL_TRIGGERED


@dataclass(frozen=True)
class EntryRequest:
    """One trigger. `context` holds the market-side inputs (prices, equity, cash, ADV, the
    calendar, gate, feed and quality flags, kill, ladder and policy versions); `open_entry`
    fills in the bot, the instrument, the signal id and the database snapshot itself.

    `skip_reason` (one of `ENTRY_SKIP_REASONS`) records a trigger whose stop or entry filters
    already failed: the signal is stored Rejected with that reason and no risk check runs.
    `criteria` is each trigger and entry-filter result, kept on `signal.triggered`."""

    bot_id: uuid.UUID
    playbook: str
    instrument_id: uuid.UUID
    trade_date: date
    triggered_at: datetime
    context: SignalContext
    last_prices: dict[uuid.UUID, Decimal] | None = None
    skip_reason: str | None = None
    criteria: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class EntryOutcome:
    signal_id: uuid.UUID
    state: str
    decision: RiskDecision | None
    position_id: uuid.UUID | None
    duplicate: bool = False
    mode: str = Mode.SHADOW


def _decision_row(decision: RiskDecision) -> dict[str, object]:
    document = decision.to_json()
    if not decision.accepted and decision.reason in ENTRY_SKIP_REASONS:
        document["stage"] = "entry_filter"
    elif not decision.accepted and decision.reason not in REASON_CODES:
        document["reason"] = "invalid_input"
    return document


def _skip_decision(reason: str) -> RiskDecision:
    """A trigger skipped before the risk check; an unknown reason is stored as invalid input."""
    return RiskDecision(
        accepted=False, reason=reason if reason in ENTRY_SKIP_REASONS else "invalid_input"
    )


def _existing_signal(session: Session, request: EntryRequest) -> HatSignal | None:
    return session.scalar(
        select(HatSignal).where(
            HatSignal.bot_id == request.bot_id,
            HatSignal.playbook == request.playbook,
            HatSignal.instrument_id == request.instrument_id,
            HatSignal.trade_date == request.trade_date,
        )
    )


def _entry_policy(session: Session, bot: HatBot, request: EntryRequest) -> PolicyVersion:
    """The policy version a new entry is decided under, after the rules that refuse it outright."""
    if request.instrument_id not in bot_universe_members(session, bot, request.trade_date):
        # Leaving the universe stops new entries (HAT-PORT-023); an open position is untouched.
        raise TradeError("not_in_universe")
    if bot.live_version_id is None:
        raise TradeError("no_live_version")
    versions: Sequence[PolicyVersion] = request.context.policy_versions or []
    policy = effective_version(versions, request.triggered_at)
    if policy is None:
        raise TradeError("no_policy_version")
    return policy


def open_entry(
    session: Session,
    request: EntryRequest,
    *,
    decide: DECIDE = evaluate_signal,
    fills: FillModel = SHADOW_FILLS,
    mode: str = Mode.SHADOW,
    issue: TokenIssuer | None = None,
    confidence: Confidence | None = None,
) -> EntryOutcome:
    """Evaluate one trigger and route an accepted one by `mode`, the bot's effective mode, in one
    transaction holding the bot's lock and then its owner's (HAT-EXEC-020, HAT-EXEC-021,
    HAT-RISK-100, HAT-RISK-030): shadow opens
    the virtual position, confirm stores the approval card (AwaitingApproval), auto leaves the
    signal in Triggered for the router to place. Every mode runs the same risk check.

    A repeated trigger for the same (bot, playbook, instrument, day) returns the existing
    signal and writes nothing. A rejection is stored as a signal in state `Rejected` with its
    reason (shown in the daily digest only, HAT-EXEC-043).

    The signal stores its confidence label (D132, HAT-EXEC-015): `confidence` when given (the
    backtest's fixed one), else the one `signal_confidence` reads, after the duplicate check and
    before the owner's lock. An auto signal whose label is not high is routed as confirm, an
    approval card for the owner (`modes.route_mode`, HAT-EXEC-014, D149)."""
    if mode not in MODE_VALUES:
        raise TradeError("unknown_mode")
    lock_bot(session, request.bot_id)
    bot = session.get(HatBot, request.bot_id)
    if bot is None:
        raise TradeError("unknown_bot")
    existing = _existing_signal(session, request)
    if existing is not None:
        # Checked before the entry rules: a retry after a mode or universe change still gets
        # the signal it already made.
        return EntryOutcome(
            existing.id, existing.state, None, None, duplicate=True, mode=existing.mode
        )
    # read before the owner's lock, so the owner's other bots never wait on it
    if confidence is None:
        confidence = entry_confidence(session, bot, request)
    if confidence.label not in LABELS:  # fail closed: an unknown label is stored as low
        confidence = Confidence.low(UNKNOWN_LABEL)
    mode = route_mode(mode, confidence.label)  # auto places only high (HAT-EXEC-014, D149)
    policy = _entry_policy(session, bot, request)
    base = request.context
    lock_owner(session, bot.user_id)  # after the duplicate check: a retry waits on no other bot
    slots = owner_slots(session, bot.user_id, request.trade_date, request.last_prices)
    entries, stopouts = day_counts(session, request.bot_id, request.trade_date)
    signal_id = uuid7()
    ctx = replace(
        base,
        signal_id=signal_id,
        bot_id=request.bot_id,
        instrument_id=request.instrument_id,
        at=request.triggered_at,
        is_virtual=mode == Mode.SHADOW,
        open_positions=slots,
        entries_today=entries,
        stopouts_today=stopouts,
        # read only when a cooldown is on (`decide` reads it under the same version)
        hard_stop_days=last_hard_stop(
            session, request.bot_id, request.instrument_id, request.triggered_at
        )
        if policy.policy.cooldown_sessions > 0
        else None,
    )
    if request.skip_reason is not None:
        decision = _skip_decision(request.skip_reason)
    else:
        decision = decide(ctx)
    accepted = bool(decision.accepted and decision.quantity and decision.stop is not None)
    entry = base.entry_price if base.entry_price is not None else _ZERO
    # The trigger price is the level the trigger broke (HAT-CRIT-119); a caller without one
    # (a backtest proxy) records the entry price.
    trigger = base.trigger_level if base.trigger_level is not None else entry
    signal = _signal_row(
        request, bot, policy, decision, signal_id, mode, trigger, accepted=accepted
    )
    signal.confidence = confidence.label
    signal.confidence_basis = dict(confidence.basis)
    session.add(signal)
    session.flush()
    payload = _triggered_payload(request, mode, trigger, entry, signal.confidence)
    journal.record(
        session,
        signal,
        journal.on_signal(signal, SIGNAL_TRIGGERED, payload, now=request.triggered_at),
    )
    if not accepted:
        reason = {"reason": signal.risk_decision["reason"]}
        journal.record(
            session,
            signal,
            journal.on_signal(signal, "signal.rejected", reason, now=request.triggered_at),
        )
        return EntryOutcome(signal.id, signal.state, decision, None, mode=mode)
    trade = Trade(signal, bot, None)
    return _route_accepted(session, trade, decision, entry, request.triggered_at, fills, issue)


def entry_confidence(session: Session, bot: HatBot, request: EntryRequest) -> Confidence:
    """The confidence label of the signal `request` makes (D132, HAT-EXEC-015)."""
    return signal_confidence(
        session,
        bot,
        request.playbook,
        request.instrument_id,
        request.trade_date,
        request.triggered_at,
    )


def _route_accepted(
    session: Session,
    trade: Trade,
    decision: RiskDecision,
    entry: Decimal,
    now: datetime,
    fills: FillModel,
    issue: TokenIssuer | None,
) -> EntryOutcome:
    """An accepted signal by its mode: confirm writes the card, auto waits for the router and
    shadow opens the virtual position."""
    signal = trade.signal
    mode = signal.mode
    if mode == Mode.CONFIRM:
        send_approval(session, signal, now, issue)
        return EntryOutcome(signal.id, signal.state, decision, None, mode=mode)
    if mode == Mode.AUTO:
        # The router places the real order after this transaction commits (HAT-BRK-101).
        return EntryOutcome(signal.id, signal.state, decision, None, mode=mode)
    qty, stop = int(decision.quantity or 0), decision.stop or _ZERO
    position = open_virtual(session, trade, qty, stop, fills.fill("buy", qty, entry), now)
    return EntryOutcome(signal.id, signal.state, decision, position.id, mode=mode)


def _signal_row(
    request: EntryRequest,
    bot: HatBot,
    policy: PolicyVersion,
    decision: RiskDecision,
    signal_id: uuid.UUID,
    mode: str,
    trigger: Decimal,
    *,
    accepted: bool,
) -> HatSignal:
    """The `hat_signal` row of one decision: a rejected signal carries no size or risk."""
    base = request.context
    stop = decision.stop if decision.stop is not None else (base.stop_price or _ZERO)
    qty = (decision.quantity or 0) if accepted else 0
    risk = (decision.risk_amount or _ZERO) if accepted else _ZERO
    return HatSignal(
        id=signal_id,
        bot_id=request.bot_id,
        strategy_version_id=bot.live_version_id,
        risk_policy_version_id=decision.policy_version_id or policy.version_id,
        playbook=request.playbook,
        instrument_id=request.instrument_id,
        trade_date=request.trade_date,
        triggered_at=request.triggered_at,
        trigger_price=trigger,
        stop_price=stop,
        qty=qty,
        risk_amount=risk,
        mode=mode,
        state=lifecycle.INITIAL_STATE,
        risk_decision=_decision_row(decision),
    )


def _triggered_payload(
    request: EntryRequest, mode: str, trigger: Decimal, entry: Decimal, confidence: str
) -> dict[str, object]:
    payload: dict[str, object] = {
        "playbook": request.playbook,
        "trigger_price": str(trigger),
        "mode": mode,
        "confidence": confidence,
    }
    if request.criteria or mode != Mode.SHADOW:
        payload["entry_price"] = str(entry)
    if request.criteria:
        payload["criteria"] = [dict(item) for item in request.criteria]
    if mode != Mode.SHADOW:
        # The percentage the Take re-check applies (HAT-EXEC-031), fixed at the trigger.
        strategy = request.context.strategy_key or ""
        pct = effective_max_chase_pct(request.context.configured_max_chase_pct, strategy)
        payload["max_chase_pct"] = str(pct)
    return payload


def send_approval(
    session: Session, signal: HatSignal, now: datetime, issue: TokenIssuer | None
) -> None:
    """The approval card of a confirm signal (HAT-EXEC-030): its `hat_approval` row, expiring
    `APPROVAL_WINDOW` after it is sent, moves the signal to AwaitingApproval in this
    transaction. The card's token comes from `issue` (the Telegram signer, HMAC-bound to the
    signal and the expiry, HAT-TG-040) and only its sha256 is stored (HAT-SCH-056). Without an
    issuer the token is random and unknown to any signer, so no button can ever take the card:
    it fails closed and expires."""
    sent = as_utc(now)  # stored in UTC: SQLite keeps no offset
    expires = sent + APPROVAL_WINDOW
    token = issue(signal.id, expires) if issue is not None else secrets.token_urlsafe(32)
    session.add(
        HatApproval(
            signal_id=signal.id,
            sent_at=sent,
            expires_at=expires,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
        )
    )
    journal.approval_sent(signal)
    session.flush()


def reject_signal(session: Session, signal_id: uuid.UUID, reason: str, now: datetime) -> bool:
    """End a routed signal whose entry was not placed (a failed Take re-check, a broker
    rejection after the single retry, or a refusal on the order path) as Rejected with a fixed
    reason, under the bot's lock. False when it already left Triggered or AwaitingApproval."""
    signal = session.get(HatSignal, signal_id)
    if signal is None:
        raise TradeError("unknown_signal")
    lock_bot(session, signal.bot_id)
    session.refresh(signal)
    if signal.state not in (lifecycle.TRIGGERED, lifecycle.AWAITING_APPROVAL):
        return False
    rejected = {"reason": reason, "stage": "route"}
    journal.record(session, signal, journal.on_signal(signal, "signal.rejected", rejected, now=now))
    return True
