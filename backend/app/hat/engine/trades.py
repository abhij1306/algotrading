"""The trade lifecycle against the database: per-bot serialisation, entries, the virtual (shadow)
fill engine, exits and the trade result (07 §4, HAT-EXEC-040..044, HAT-SCH-057, HAT-BOT-083).

Every function works in the caller's open transaction and never commits. A state change and the
event that explains it are written together, so a replay of the events always reproduces the
stored state (`replay_state`).

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
`import_graph_boundaries`). Exits here are virtual; real exits are the router's.

**Veto trades (HAT-EXEC-032).** A confirm signal that is skipped or expires continues as a
virtual trade for the veto score: `open_veto_trade` opens it on the signal's own row, which has
reached its ending (Cancelled or Expired). Its events carry the signal id as every trade event
does (HAT-SCH-064) and `veto: true` in their payload; they move only the veto position's state,
so `replay_state` folds the signal's own events and `replay_veto_state` the veto trade's, which
starts at EntryPlaced with its virtual order.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from ..errors import HatError
from ..events import record
from ..execution.tags import encode_tag
from ..ids import uuid7
from ..models import (
    HatApproval,
    HatBot,
    HatEvent,
    HatFill,
    HatIndustry,
    HatOrder,
    HatPosition,
    HatSignal,
    HatStrategyVersion,
    HatTradeResult,
    HatWatchlistEntry,
)
from ..risk import controls
from ..risk.caps import OpenPosition, effective_max_chase_pct
from ..risk.decision import REASON_CODES, RiskDecision, SignalContext
from ..risk.policy import PolicyVersion, effective_version
from ..risk.policy_service import load_versions
from ..risk.service import evaluate_signal
from ..time import as_utc, day_bounds, trade_date_of
from . import exits, lifecycle, rescale
from .botlock import lock_bot, lock_owner
from .stops import SKIP_BAD_INPUT, SKIP_NOT_BELOW_ENTRY, SKIP_TOO_NARROW, SKIP_TOO_WIDE
from .strategy.config import ExitRule, parse_config
from .watchlist import bot_universe_members

__all__ = [
    "APPROVAL_WINDOW",
    "SHADOW_FILLS",
    "EntryOutcome",
    "EntryRequest",
    "FillModel",
    "TradeError",
    "owner_slots",
    "bot_snapshot",
    "close_position",
    "entry_price_of",
    "evaluate_position",
    "expire_watchlist",
    "lock_bot",
    "open_entry",
    "open_veto_trade",
    "position_state",
    "reject_signal",
    "replay_state",
    "replay_veto_state",
    "risk_inputs",
    "trade_r",
]

logger = logging.getLogger(__name__)
SHADOW = "shadow"
CONFIRM = "confirm"
AUTO = "auto"
MODES = (SHADOW, CONFIRM, AUTO)
DECIDE = Callable[[SignalContext], RiskDecision]
# HAT-EXEC-030: an approval card expires 5 minutes after it is sent.
APPROVAL_WINDOW = timedelta(minutes=5)
# Issues an approval card's token from (signal id, expires_at); `telegram.tokens.ApprovalSigner`.
TokenIssuer = Callable[[uuid.UUID, datetime], str]
VETO = "veto"  # the payload flag of a veto trade's events
# An accepted auto signal rests in Triggered until the router places its order: it holds a slot.
_SLOT_PENDING_STATES = (lifecycle.TRIGGERED, lifecycle.AWAITING_APPROVAL, lifecycle.ENTRY_PLACED)
_ENTERED_STATES = (
    lifecycle.TRIGGERED,
    lifecycle.AWAITING_APPROVAL,
    lifecycle.ENTRY_PLACED,
    lifecycle.OPEN,
    lifecycle.PARTIALLY_EXITED,
    lifecycle.EXIT_PENDING,
    lifecycle.CLOSED,
)
_POSITION_STATES = (
    lifecycle.OPEN,
    lifecycle.PARTIALLY_EXITED,
    lifecycle.EXIT_PENDING,
    lifecycle.CLOSED,
)
HARD_STOP = "hard_stop"
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
SIGNAL_TRIGGERED = "signal.triggered"
_RATIO_PLACES = Decimal("0.000001")


class FillModel(Protocol):
    """How a virtual fill is priced: (fill price, fees) for `qty` shares at the reference
    `price` on `side` ("buy" or "sell")."""

    def fill(self, side: str, qty: int, price: Decimal, /) -> tuple[Decimal, Decimal]: ...


class _ShadowFills:
    """Shadow trading's fills: at the reference price, with no fees (PR 11)."""

    def fill(self, _side: str, _qty: int, price: Decimal, /) -> tuple[Decimal, Decimal]:
        return price, _ZERO


# The backtest passes its own model (slippage and charges, `app.hat.backtest.costs`); the shadow
# engine and the backtest otherwise fill through the same functions (HAT-BT-010).
SHADOW_FILLS: FillModel = _ShadowFills()


class TradeError(HatError):
    """A lifecycle operation was refused; `code` is a fixed word and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- the snapshot the caps read ---------------------------------------------------------------


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    return day_bounds(day)


def _industry_of(session: Session, instrument_id: uuid.UUID, day: date) -> str:
    row = session.scalar(
        select(HatIndustry.industry)
        .where(HatIndustry.instrument_id == instrument_id, HatIndustry.effective_from <= day)
        .order_by(HatIndustry.effective_from.desc())
        .limit(1)
    )
    return row or "unknown"


# A veto trade is a virtual position of a confirm or auto signal (see `_is_veto`).
_NOT_VETO = or_(HatPosition.is_virtual.is_(False), HatSignal.mode == SHADOW)


def _slots(
    session: Session,
    bot_ids: Sequence[uuid.UUID] | Select[tuple[uuid.UUID]],
    trade_date: date,
    last_prices: dict[uuid.UUID, Decimal] | None,
) -> list[OpenPosition]:
    """The slots of the bots in `bot_ids`, read now, each carrying its own bot.

    A slot is an open position, or a signal whose entry is triggered, placed or awaiting
    approval and has no position yet. A veto trade (the virtual trade of a skipped or expired
    confirm signal, HAT-EXEC-032) is scored only and holds no slot. A position with no price in
    `last_prices` is valued at its entry.

    The pending signals are read before the positions: another bot's fill (under that bot's
    lock only) can turn a pending signal into a position between the two reads, and in this
    order it is then counted twice, never missed."""
    prices = last_prices or {}
    pending = session.scalars(
        select(HatSignal).where(
            HatSignal.bot_id.in_(bot_ids), HatSignal.state.in_(_SLOT_PENDING_STATES)
        )
    ).all()
    slots: list[OpenPosition] = []
    for position in session.scalars(
        select(HatPosition)
        .outerjoin(HatSignal, HatSignal.id == HatPosition.signal_id)
        .where(HatPosition.bot_id.in_(bot_ids), HatPosition.closed_at.is_(None), _NOT_VETO)
    ):
        entry = Decimal(position.avg_entry)
        slots.append(
            OpenPosition(
                bot_id=position.bot_id,
                instrument_id=position.instrument_id,
                industry=_industry_of(session, position.instrument_id, trade_date),
                qty=position.qty_open,
                avg_entry=entry,
                last_price=prices.get(position.instrument_id, entry),
                current_stop=Decimal(position.current_stop),
                is_virtual=position.is_virtual,
            )
        )
    for signal in pending:
        entry = entry_price_of(session, signal)  # the planned price, not the level broken
        slots.append(
            OpenPosition(
                bot_id=signal.bot_id,
                instrument_id=signal.instrument_id,
                industry=_industry_of(session, signal.instrument_id, trade_date),
                qty=signal.qty,
                avg_entry=entry,
                last_price=prices.get(signal.instrument_id, entry),
                current_stop=Decimal(signal.stop_price),
                is_virtual=signal.mode == SHADOW,
            )
        )
    return slots


def owner_slots(
    session: Session,
    user_id: uuid.UUID,
    trade_date: date,
    last_prices: dict[uuid.UUID, Decimal] | None = None,
) -> list[OpenPosition]:
    """The slots of every bot of the owner, real and virtual, on every broker account
    (HAT-RISK-030): the global open-risk cap sums the ones of the entry's mode, the per-bot caps
    the entry's bot's own."""
    bots = select(HatBot.id).where(HatBot.user_id == user_id)
    return _slots(session, bots, trade_date, last_prices)


def bot_snapshot(
    session: Session,
    bot_id: uuid.UUID,
    trade_date: date,
    last_prices: dict[uuid.UUID, Decimal] | None = None,
) -> tuple[list[OpenPosition], int, int]:
    """(slots, entries today, stop-outs today) for the bot, read now (slots as `_slots`). A veto
    trade's stop-out is not the bot's. These are the bot's own slots only: an entry decision
    reads `owner_slots`, since the global open-risk cap sums every bot."""
    slots = _slots(session, [bot_id], trade_date, last_prices)
    entries, stopouts = _day_counts(session, bot_id, trade_date)
    return slots, entries, stopouts


def _day_counts(session: Session, bot_id: uuid.UUID, trade_date: date) -> tuple[int, int]:
    """(entries today, stop-outs today) for the bot."""
    entries = session.scalar(
        select(func.count())
        .select_from(HatSignal)
        .where(
            HatSignal.bot_id == bot_id,
            HatSignal.trade_date == trade_date,
            HatSignal.state.in_(_ENTERED_STATES),
        )
    )
    start, end = _day_bounds(trade_date)
    stopouts = session.scalar(
        select(func.count())
        .select_from(HatTradeResult)
        .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
        .outerjoin(HatSignal, HatSignal.id == HatPosition.signal_id)
        .where(
            HatPosition.bot_id == bot_id,
            HatPosition.closed_at >= start,
            HatPosition.closed_at < end,
            HatTradeResult.exit_reason == HARD_STOP,
            _NOT_VETO,
        )
    )
    return int(entries or 0), int(stopouts or 0)


def risk_inputs(session: Session, bot_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, object]:
    """The fields of a `SignalContext` that the database holds: the kill state, the exposure
    ladder and the risk-policy versions."""
    return {
        "kill": controls.kill_state(session, bot_id),
        "ladder": controls.ladder_state(session, bot_id),
        "policy_versions": load_versions(session, user_id),
    }


# --- events and steps -------------------------------------------------------------------------


def _emit(
    session: Session,
    signal: HatSignal,
    event_type: str,
    *,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    payload: dict[str, object],
    now: datetime,
    position: HatPosition | None = None,
    veto: bool = False,
) -> None:
    """Record `event_type` and, when it drives a lifecycle step, move the signal (and its
    position) to the next state in the same transaction (HAT-EXEC-042). A veto trade's event is
    flagged `veto` and moves only its position: the signal has already ended."""
    step = lifecycle.step_of(event_type, payload)
    if veto:
        payload = {**payload, VETO: True}
        if step is not None and position is not None:
            # The veto position is stored once it opens; before that its trade is EntryPlaced.
            before = lifecycle.ENTRY_PLACED if event_type == "position.opened" else position.state
            position.state = lifecycle.next_state(before, step)
    elif step is not None:
        after = lifecycle.next_state(signal.state, step)
        signal.state = after
        if position is not None and after in _POSITION_STATES:
            position.state = after
    record(
        session,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        actor="engine",
        payload=payload,
        correlation_id=signal.id,
        bot_id=signal.bot_id,
        occurred_at=now,
    )


def _trade_steps(session: Session, signal_id: uuid.UUID, *, veto: bool) -> list[str]:
    rows = session.execute(
        select(HatEvent.event_type, HatEvent.payload)
        .where(HatEvent.correlation_id == signal_id)
        .order_by(HatEvent.id)
    ).all()
    steps = []
    for event_type, payload in rows:
        if bool((payload or {}).get(VETO)) != veto:
            continue
        step = lifecycle.step_of(event_type, payload)
        if step is not None:
            steps.append(step)
    return steps


def replay_state(session: Session, signal_id: uuid.UUID) -> str:
    """The state the signal's events give when folded over the transition table. The approval
    card has no event of its own: its `hat_approval` row, written in the transaction that moved
    the signal to AwaitingApproval, stands for the step right after `signal.triggered`."""
    steps = _trade_steps(session, signal_id, veto=False)
    if session.get(HatApproval, signal_id) is not None and steps[:1] == [SIGNAL_TRIGGERED]:
        steps.insert(1, "approval.sent")
    return lifecycle.fold(steps)


def replay_veto_state(session: Session, signal_id: uuid.UUID) -> str | None:
    """The state of the signal's veto trade folded from its events (it starts at Triggered, so
    its virtual order moves it to EntryPlaced), or None when it has none."""
    steps = _trade_steps(session, signal_id, veto=True)
    return lifecycle.fold(steps, start=lifecycle.TRIGGERED) if steps else None


def _next_tag(session: Session, signal_id: uuid.UUID, purpose: str) -> str:
    count = session.scalar(
        select(func.count())
        .select_from(HatOrder)
        .where(HatOrder.signal_id == signal_id, HatOrder.purpose == purpose)
    )
    return encode_tag(signal_id, purpose, int(count or 0))


# --- entries ----------------------------------------------------------------------------------


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
    mode: str = SHADOW


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
    mode: str = SHADOW,
    issue: TokenIssuer | None = None,
) -> EntryOutcome:
    """Evaluate one trigger and route an accepted one by `mode`, the bot's effective mode, in one
    transaction holding the bot's lock and then its owner's (HAT-EXEC-020, HAT-EXEC-021,
    HAT-RISK-100, HAT-RISK-030): shadow opens
    the virtual position, confirm stores the approval card (AwaitingApproval), auto leaves the
    signal in Triggered for the router to place. Every mode runs the same risk check.

    A repeated trigger for the same (bot, playbook, instrument, day) returns the existing
    signal and writes nothing. A rejection is stored as a signal in state `Rejected` with its
    reason (shown in the daily digest only, HAT-EXEC-043)."""
    if mode not in MODES:
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
    policy = _entry_policy(session, bot, request)
    base = request.context
    lock_owner(session, bot.user_id)  # after the duplicate check: a retry waits on no other bot
    slots = owner_slots(session, bot.user_id, request.trade_date, request.last_prices)
    entries, stopouts = _day_counts(session, request.bot_id, request.trade_date)
    signal_id = uuid7()
    ctx = replace(
        base,
        signal_id=signal_id,
        bot_id=request.bot_id,
        instrument_id=request.instrument_id,
        at=request.triggered_at,
        is_virtual=mode == SHADOW,
        open_positions=slots,
        entries_today=entries,
        stopouts_today=stopouts,
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
    session.add(signal)
    session.flush()
    _emit(
        session,
        signal,
        SIGNAL_TRIGGERED,
        aggregate_type="signal",
        aggregate_id=signal.id,
        payload=_triggered_payload(request, mode, trigger, entry),
        now=request.triggered_at,
    )
    if not accepted:
        _emit(
            session,
            signal,
            "signal.rejected",
            aggregate_type="signal",
            aggregate_id=signal.id,
            payload={"reason": signal.risk_decision["reason"]},
            now=request.triggered_at,
        )
        return EntryOutcome(signal.id, signal.state, decision, None, mode=mode)
    return _route_accepted(
        session, signal, bot, decision, entry, request.triggered_at, fills, issue
    )


def _route_accepted(
    session: Session,
    signal: HatSignal,
    bot: HatBot,
    decision: RiskDecision,
    entry: Decimal,
    now: datetime,
    fills: FillModel,
    issue: TokenIssuer | None,
) -> EntryOutcome:
    """An accepted signal by its mode: confirm writes the card, auto waits for the router and
    shadow opens the virtual position."""
    mode = signal.mode
    if mode == CONFIRM:
        _send_approval(session, signal, now, issue)
        return EntryOutcome(signal.id, signal.state, decision, None, mode=mode)
    if mode == AUTO:
        # The router places the real order after this transaction commits (HAT-BRK-101).
        return EntryOutcome(signal.id, signal.state, decision, None, mode=mode)
    qty, stop = int(decision.quantity or 0), decision.stop or _ZERO
    position = _virtual_entry(session, signal, bot, qty, stop, fills.fill("buy", qty, entry), now)
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
        currency="INR",
        mode=mode,
        state=lifecycle.INITIAL_STATE,
        risk_decision=_decision_row(decision),
    )


def _triggered_payload(
    request: EntryRequest, mode: str, trigger: Decimal, entry: Decimal
) -> dict[str, object]:
    payload: dict[str, object] = {
        "playbook": request.playbook,
        "trigger_price": str(trigger),
        "mode": mode,
    }
    if request.criteria or mode != SHADOW:
        payload["entry_price"] = str(entry)
    if request.criteria:
        payload["criteria"] = [dict(item) for item in request.criteria]
    if mode != SHADOW:
        # The percentage the Take re-check applies (HAT-EXEC-031), fixed at the trigger.
        strategy = request.context.strategy_key or ""
        pct = effective_max_chase_pct(request.context.configured_max_chase_pct, strategy)
        payload["max_chase_pct"] = str(pct)
    return payload


def _send_approval(
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
    signal.state = lifecycle.next_state(signal.state, "approval.sent")
    session.flush()


def _virtual_order(
    session: Session,
    signal: HatSignal,
    bot: HatBot,
    position: HatPosition | None,
    *,
    purpose: str,
    side: str,
    qty: int,
    fill: tuple[Decimal, Decimal],
    now: datetime,
) -> tuple[HatOrder, HatFill]:
    """A virtual order and its fill at `fill` (price, fees): no broker id, no broker trade id."""
    price, fees = fill
    order = HatOrder(
        id=uuid7(),
        signal_id=signal.id,
        position_id=None if position is None else position.id,
        broker_account_id=bot.broker_account_id,
        purpose=purpose,
        side=side,
        qty=qty,
        order_type="market",
        tag=_next_tag(session, signal.id, purpose),
        broker_order_id=None,
        status="filled",
        is_virtual=True,
        placed_at=now,
        currency="INR",
    )
    session.add(order)
    session.flush()
    fill = HatFill(
        id=uuid7(),
        order_id=order.id,
        qty=qty,
        price=price,
        fees=fees,
        currency="INR",
        filled_at=now,
        broker_trade_id=None,
        is_virtual=True,
    )
    session.add(fill)
    session.flush()
    return order, fill


def _virtual_entry(
    session: Session,
    signal: HatSignal,
    bot: HatBot,
    qty: int,
    stop: Decimal,
    fill: tuple[Decimal, Decimal],
    now: datetime,
    *,
    veto: bool = False,
) -> HatPosition:
    entry = fill[0]
    order, _ = _virtual_order(
        session, signal, bot, None, purpose="entry", side="buy", qty=qty, fill=fill, now=now
    )
    _emit(
        session,
        signal,
        "order.placed",
        aggregate_type="order",
        aggregate_id=order.id,
        payload={"purpose": "entry", "qty": qty, "is_virtual": True, "tag": order.tag},
        now=now,
        veto=veto,
    )
    _emit(
        session,
        signal,
        "order.filled",
        aggregate_type="order",
        aggregate_id=order.id,
        payload={"qty": qty, "price": str(entry), "is_virtual": True},
        now=now,
        veto=veto,
    )
    position = HatPosition(
        id=uuid7(),
        bot_id=bot.id,
        signal_id=signal.id,
        instrument_id=signal.instrument_id,
        is_virtual=True,
        qty_open=qty,
        avg_entry=entry,
        initial_stop=stop,
        current_stop=stop,
        currency="INR",
        opened_at=now,
        state=lifecycle.OPEN,
        rules_fired=[],
    )
    session.add(position)
    session.flush()  # the position row first: the order's foreign key points at it
    order.position_id = position.id
    session.flush()
    _emit(
        session,
        signal,
        "position.opened",
        aggregate_type="position",
        aggregate_id=position.id,
        payload={"qty": qty, "avg_entry": str(entry), "stop": str(stop), "is_virtual": True},
        now=now,
        position=position,
        veto=veto,
    )
    return position


# --- the router's steps on a confirm or auto signal -------------------------------------------


def entry_price_of(session: Session, signal: HatSignal) -> Decimal:
    """The entry price the trigger planned, from its `signal.triggered` event (else the level)."""
    payload = session.scalar(
        select(HatEvent.payload).where(
            HatEvent.correlation_id == signal.id, HatEvent.event_type == SIGNAL_TRIGGERED
        )
    )
    raw = (payload or {}).get("entry_price")
    price = Decimal(str(raw)) if raw is not None else Decimal(signal.trigger_price)
    return price if price.is_finite() and price > 0 else Decimal(signal.trigger_price)


def _veto_position(session: Session, signal_id: uuid.UUID) -> HatPosition | None:
    return session.scalar(
        select(HatPosition).where(
            HatPosition.signal_id == signal_id, HatPosition.is_virtual.is_(True)
        )
    )


def open_veto_trade(session: Session, signal_id: uuid.UUID, now: datetime) -> HatPosition | None:
    """Open the virtual trade of a skipped or expired confirm signal for the veto score
    (HAT-EXEC-032, HAT-TG-045), at the entry price, quantity and stop the card offered, under
    the bot's lock. At most one per signal: a second call returns the one already open. None
    when the signal is not a decided-away confirm signal or was never sized."""
    signal = session.get(HatSignal, signal_id)
    if signal is None:
        raise TradeError("unknown_signal")
    lock_bot(session, signal.bot_id)
    session.refresh(signal)
    approval = session.get(HatApproval, signal_id)
    if (
        signal.mode != CONFIRM
        or approval is None
        or approval.decision not in ("skip", "expired")
        or signal.state not in (lifecycle.CANCELLED, lifecycle.EXPIRED)
        or signal.qty <= 0
    ):
        return None
    existing = _veto_position(session, signal_id)
    if existing is not None:
        return existing
    bot = session.get(HatBot, signal.bot_id)
    if bot is None:
        raise TradeError("unknown_bot")
    fill = SHADOW_FILLS.fill("buy", signal.qty, entry_price_of(session, signal))
    return _virtual_entry(
        session, signal, bot, signal.qty, Decimal(signal.stop_price), fill, now, veto=True
    )


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
    _emit(
        session,
        signal,
        "signal.rejected",
        aggregate_type="signal",
        aggregate_id=signal.id,
        payload={"reason": reason, "stage": "route"},
        now=now,
    )
    return True


# --- exits ------------------------------------------------------------------------------------


def exit_rules_of(session: Session, signal: HatSignal) -> list[ExitRule]:
    """The exit stack of the strategy version the signal entered under: a version promoted
    after the entry never changes the rules of an open trade (HAT-BOT-083)."""
    version = session.get(HatStrategyVersion, signal.strategy_version_id)
    if version is None:
        raise TradeError("unknown_strategy_version")
    return list(parse_config(version.config).exits)


def _entry_qty(session: Session, signal: HatSignal, position: HatPosition) -> int:
    """The shares the entry bought, on the position's current scale: each split or bonus applied
    to it since (`position.rescaled`) scales them as it scaled `qty_open`, so R and the partial
    exits stay on one scale (HAT-EXEC-141)."""
    qty = int(
        session.scalar(
            select(func.coalesce(func.sum(HatFill.qty), 0))
            .join(HatOrder, HatOrder.id == HatFill.order_id)
            .where(HatOrder.signal_id == signal.id, HatOrder.purpose == "entry")
        )
        or 0
    )
    for factor in rescale.applied_factors(session, position.id):
        qty = rescale.scale_qty(qty, factor)
    return qty


def trade_r(realized_pnl: Decimal, avg_entry: Decimal, initial_stop: Decimal, qty: int) -> Decimal:
    """HAT-BT-040: realised P&L over the initial risk (entry - initial stop) x entry quantity.
    Zero when there is no positive initial risk (the position was never validly sized)."""
    risk = (avg_entry - initial_stop) * qty
    if risk <= 0:
        return _ZERO
    return (realized_pnl / risk).quantize(_RATIO_PLACES)


def _is_veto(signal: HatSignal, position: HatPosition) -> bool:
    """A virtual position of a confirm or auto signal is that signal's veto trade."""
    return position.is_virtual and signal.mode != SHADOW


def _locked_virtual_position(session: Session, position_id: uuid.UUID) -> HatPosition:
    """The position, re-read under its bot's lock; only a virtual one is the engine's."""
    position = session.get(HatPosition, position_id)
    if position is None:
        raise TradeError("unknown_position")
    lock_bot(session, position.bot_id)
    session.refresh(position)
    if not position.is_virtual:
        raise TradeError("position_not_virtual")  # real exits are the router's (PR 15)
    return position


def _raise_stop(
    session: Session,
    signal: HatSignal,
    position: HatPosition,
    old: Decimal,
    new: Decimal,
    now: datetime,
) -> None:
    _emit(
        session,
        signal,
        "stop.moved",
        aggregate_type="position",
        aggregate_id=position.id,
        payload={"from": str(old), "to": str(new)},
        now=now,
        veto=_is_veto(signal, position),
    )
    position.current_stop = new


def _signal_and_bot(session: Session, position: HatPosition) -> tuple[HatSignal, HatBot]:
    signal = session.get(HatSignal, position.signal_id)
    bot = session.get(HatBot, position.bot_id)
    if signal is None or bot is None:
        raise TradeError("unknown_signal")
    return signal, bot


def position_state(
    session: Session, position: HatPosition
) -> tuple[list[ExitRule], exits.PositionState]:
    """The pinned exit stack of a position and the position as the exit rules read it: what
    `evaluate_position` hands to `exits.evaluate`."""
    signal, _ = _signal_and_bot(session, position)
    state = exits.PositionState(
        qty_original=_entry_qty(session, signal, position),
        qty_open=position.qty_open,
        avg_entry=Decimal(position.avg_entry),
        initial_stop=Decimal(position.initial_stop),
        current_stop=Decimal(position.current_stop),
        fired=frozenset(position.rules_fired or []),
    )
    return exit_rules_of(session, signal), state


def evaluate_position(
    session: Session,
    position_id: uuid.UUID,
    view: exits.ExitView,
    phase: exits.Phase,
    now: datetime,
    *,
    high: Decimal | None = None,
    low: Decimal | None = None,
    fills: FillModel = SHADOW_FILLS,
) -> exits.Evaluation:
    """Run the pinned exit stack on one virtual position and carry out what it says: raise the
    stop, sell partials, or close the position and write its trade result (HAT-EXEC-070..073).

    `high` and `low` are the extremes the price made since entry, for the result's MFE and MAE.
    A sale fills at `view.price` through `fills`."""
    position = _locked_virtual_position(session, position_id)
    if position.closed_at is not None or position.qty_open <= 0:
        return exits.Evaluation()
    signal, bot = _signal_and_bot(session, position)
    rules, state = position_state(session, position)
    result = exits.evaluate(rules, state, view, phase)
    price = view.price
    if result.actions and price is None:
        raise TradeError("exit_without_price")  # nothing is written for an exit with no price
    if result.new_stop is not None and result.new_stop > state.current_stop:
        _raise_stop(session, signal, position, state.current_stop, result.new_stop, now)
    for rule_id, reason in result.ignored:
        # A rule that would not raise the stop is logged, never applied (HAT-RISK-070).
        _log_ignored(rule_id, reason)
    position.rules_fired = sorted({*(position.rules_fired or []), *result.fired})
    if price is not None:
        marks = _Marks(fills, high, low)
        for action in result.actions:
            sale = _Sale(action.kind == "full", action.qty, action.rule_ids[0].split(":", 1)[1])
            _virtual_exit(session, signal, bot, position, sale, price, now, marks)
    session.flush()
    return result


def close_position(
    session: Session,
    position_id: uuid.UUID,
    price: Decimal,
    reason: str,
    now: datetime,
    *,
    high: Decimal | None = None,
    low: Decimal | None = None,
    fills: FillModel = SHADOW_FILLS,
) -> bool:
    """Sell the whole open quantity of a virtual position at `price` for a `reason` outside the
    exit stack (the backtest's period end, an instrument's last traded day) and write its trade
    result. False when the position was already closed."""
    position = _locked_virtual_position(session, position_id)
    if position.closed_at is not None or position.qty_open <= 0:
        return False
    signal, bot = _signal_and_bot(session, position)
    sale = _Sale(True, position.qty_open, reason)
    _virtual_exit(session, signal, bot, position, sale, price, now, _Marks(fills, high, low))
    session.flush()
    return True


def _log_ignored(rule_id: str, reason: str) -> None:
    logger.info("exit rule %s ignored: %s", rule_id, reason)


@dataclass(frozen=True)
class _Sale:
    full: bool
    qty: int
    rule: str


@dataclass(frozen=True)
class _Marks:
    """How a sale fills, and the extremes since entry for the trade result."""

    fills: FillModel
    high: Decimal | None
    low: Decimal | None


def _virtual_exit(
    session: Session,
    signal: HatSignal,
    bot: HatBot,
    position: HatPosition,
    sale: _Sale,
    price: Decimal,
    now: datetime,
    marks: _Marks,
) -> None:
    full = sale.full
    veto = _is_veto(signal, position)
    purpose = "exit_full" if full else "exit_partial"
    fill = marks.fills.fill("sell", sale.qty, price)
    filled = fill[0]
    order, _ = _virtual_order(
        session,
        signal,
        bot,
        position,
        purpose=purpose,
        side="sell",
        qty=sale.qty,
        fill=fill,
        now=now,
    )
    rule = sale.rule
    _emit(
        session,
        signal,
        "order.placed",
        aggregate_type="order",
        aggregate_id=order.id,
        payload={"purpose": purpose, "qty": sale.qty, "is_virtual": True, "rule": rule},
        now=now,
        position=position,
        veto=veto,
    )
    _emit(
        session,
        signal,
        "order.filled",
        aggregate_type="order",
        aggregate_id=order.id,
        payload={"qty": sale.qty, "price": str(filled), "is_virtual": True},
        now=now,
        veto=veto,
    )
    position.qty_open -= sale.qty
    if not full:
        _emit(
            session,
            signal,
            "exit.partial",
            aggregate_type="position",
            aggregate_id=position.id,
            payload={"qty": sale.qty, "price": str(filled), "rule": rule},
            now=now,
            position=position,
            veto=veto,
        )
        return
    position.closed_at = now
    _emit(
        session,
        signal,
        "position.closed",
        aggregate_type="position",
        aggregate_id=position.id,
        payload={"price": str(filled), "rule": rule},
        now=now,
        position=position,
        veto=veto,
    )
    _write_result(session, signal, position, rule, now, high=marks.high, low=marks.low)


def _write_result(
    session: Session,
    signal: HatSignal,
    position: HatPosition,
    reason: str,
    now: datetime,
    *,
    high: Decimal | None,
    low: Decimal | None,
) -> None:
    """The one `hat_trade_result` of a closed position, in the closing transaction
    (HAT-SCH-057), and the ladder step for a real trade (a virtual one changes nothing)."""
    fills = session.execute(
        select(HatOrder.side, HatFill.qty, HatFill.price, HatFill.fees)
        .join(HatOrder, HatOrder.id == HatFill.order_id)
        .where(HatOrder.signal_id == signal.id)
    ).all()
    pnl = _ZERO
    fees = _ZERO
    for side, qty, price, fee in fills:
        sign = 1 if side == "sell" else -1
        pnl += sign * qty * Decimal(price) - Decimal(fee)
        fees += Decimal(fee)
    entry = Decimal(position.avg_entry)
    stop = Decimal(position.initial_stop)
    qty = _entry_qty(session, signal, position)
    risk = entry - stop
    r = trade_r(pnl, entry, stop, qty)
    top = max(entry, high if high is not None else entry)
    bottom = min(entry, low if low is not None else entry)
    session.add(
        HatTradeResult(
            position_id=position.id,
            realized_pnl=pnl,
            fees=fees,
            currency="INR",
            r_multiple=r,
            holding_days=max(0, (trade_date_of(now) - signal.trade_date).days),
            exit_reason=reason,
            mfe_r=((top - entry) / risk).quantize(_RATIO_PLACES) if risk > 0 else _ZERO,
            mae_r=((bottom - entry) / risk).quantize(_RATIO_PLACES) if risk > 0 else _ZERO,
        )
    )
    policy = effective_version(load_versions(session, _owner_of(session, signal)), now)
    if policy is not None:
        controls.record_closed_trade(session, signal.bot_id, r, position.is_virtual, policy.policy)


def _owner_of(session: Session, signal: HatSignal) -> uuid.UUID:
    bot = session.get(HatBot, signal.bot_id)
    if bot is None:
        raise TradeError("unknown_bot")
    return bot.user_id


# --- the end of the session -------------------------------------------------------------------


def expire_watchlist(session: Session, trade_date: date) -> int:
    """At the session end of `trade_date`, a watchlist row that never triggered ends as Expired
    (HAT-EXEC-041). Rows that triggered keep the status their signal gave them."""
    triggered = select(HatSignal.bot_id, HatSignal.playbook, HatSignal.instrument_id).where(
        HatSignal.trade_date == trade_date
    )
    keys = {tuple(row) for row in session.execute(triggered)}
    count = 0
    for entry in session.scalars(
        select(HatWatchlistEntry).where(
            HatWatchlistEntry.trade_date == trade_date,
            HatWatchlistEntry.status == lifecycle.WATCHLISTED,
        )
    ):
        if (entry.bot_id, entry.playbook, entry.instrument_id) in keys:
            entry.status = lifecycle.TRIGGERED
            continue
        entry.status = lifecycle.next_state(entry.status, "watchlist.expired")
        count += 1
    return count
