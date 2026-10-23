"""The trade lifecycle against the database: per-bot serialisation, entries, the approval card,
the virtual (shadow) order, fill and exit engine and veto trades (HAT-EXEC-040..044,
HAT-SCH-057, HAT-BOT-083). The reads an entry decision takes are `snapshot`'s; what the shadow
engine shares with real positions (`live_trades`), the exit stack's inputs, a stop raise and the
trade result, is `positions`' (PR 75a).

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
`import_graph_boundaries`). Exits here are virtual; real exits are the router's.

**Veto trades (HAT-EXEC-032).** A confirm signal that is skipped or expires continues as a
virtual trade for the veto score: `open_veto_trade` opens it on the signal's own row, which has
reached its ending (Cancelled or Expired). Its events carry the signal id as every trade event
does (HAT-SCH-064) and `veto: true` in their payload; they move only the veto position's state,
so `journal.replay_state` folds the signal's own events and `journal.replay_veto_state` the veto
trade's, which starts at EntryPlaced with its virtual order. Every state change goes through the
journal (`engine/journal.py`, PR 36).
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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..execution.modes import route_mode
from ..execution.tags import encode_tag
from ..ids import uuid7
from ..models import (
    HatApproval,
    HatBot,
    HatFill,
    HatOrder,
    HatPosition,
    HatSignal,
    HatWatchlistEntry,
)
from ..risk.caps import effective_max_chase_pct
from ..risk.decision import REASON_CODES, RiskDecision, SignalContext
from ..risk.modes import MODE_VALUES, Mode
from ..risk.policy import PolicyVersion, effective_version
from ..risk.service import evaluate_signal
from ..time import as_utc
from . import exits, journal, lifecycle, rescale
from .botlock import lock_bot, lock_owner
from .confidence import LABELS, UNKNOWN_LABEL, Confidence
from .positions import TradeError, is_veto, position_state, raise_stop, signal_and_bot, write_result
from .snapshot import day_counts, entry_price_of, last_hard_stop, owner_slots
from .stops import SKIP_BAD_INPUT, SKIP_NOT_BELOW_ENTRY, SKIP_TOO_NARROW, SKIP_TOO_WIDE
from .watchlist import bot_universe_members

__all__ = [
    "APPROVAL_WINDOW",
    "SHADOW_FILLS",
    "EntryOutcome",
    "EntryRequest",
    "FillModel",
    "close_position",
    "evaluate_position",
    "expire_watchlist",
    "lock_bot",
    "open_entry",
    "open_veto_trade",
    "reject_signal",
    "send_approval",
]

logger = logging.getLogger(__name__)
DECIDE = Callable[[SignalContext], RiskDecision]
# HAT-EXEC-030: an approval card expires 5 minutes after it is sent.
APPROVAL_WINDOW = timedelta(minutes=5)
# Issues an approval card's token from (signal id, expires_at); `telegram.tokens.ApprovalSigner`.
TokenIssuer = Callable[[uuid.UUID, datetime], str]
VETO = journal.VETO  # the payload flag of a veto trade's events
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
    journal.record(
        session,
        signal,
        SIGNAL_TRIGGERED,
        aggregate_type="signal",
        aggregate_id=signal.id,
        payload=_triggered_payload(request, mode, trigger, entry, signal.confidence),
        now=request.triggered_at,
    )
    if not accepted:
        journal.record(
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


def entry_confidence(session: Session, bot: HatBot, request: EntryRequest) -> Confidence:
    """The confidence label of the signal `request` makes (D132, HAT-EXEC-015)."""
    # imported here: the backtest runs module imports the backtest engine, which imports this one
    from ..backtest.runs import signal_confidence  # noqa: PLC0415

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
    if mode == Mode.CONFIRM:
        send_approval(session, signal, now, issue)
        return EntryOutcome(signal.id, signal.state, decision, None, mode=mode)
    if mode == Mode.AUTO:
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
    journal.record(
        session,
        signal,
        "order.placed",
        aggregate_type="order",
        aggregate_id=order.id,
        payload={"purpose": "entry", "qty": qty, "is_virtual": True, "tag": order.tag},
        now=now,
        veto=veto,
    )
    journal.record(
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
    journal.record(
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
        signal.mode != Mode.CONFIRM
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
    journal.record(
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
    signal, bot = signal_and_bot(session, position)
    rules, state = position_state(session, position)
    result = exits.evaluate(rules, state, view, phase)
    price = view.price
    if result.actions and price is None:
        raise TradeError("exit_without_price")  # nothing is written for an exit with no price
    if result.new_stop is not None and result.new_stop > state.current_stop:
        raise_stop(session, signal, position, state.current_stop, result.new_stop, now)
    for rule_id, reason in result.ignored:
        # A rule that would not raise the stop is logged, never applied (HAT-RISK-070).
        logger.info("exit rule %s ignored: %s", rule_id, reason)
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
    signal, bot = signal_and_bot(session, position)
    sale = _Sale(True, position.qty_open, reason)
    _virtual_exit(session, signal, bot, position, sale, price, now, _Marks(fills, high, low))
    session.flush()
    return True


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
    veto = is_veto(signal, position)
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
    journal.record(
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
    journal.record(
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
        journal.record(
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
    journal.record(
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
    write_result(session, signal, position, rule, now, high=marks.high, low=marks.low)


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
