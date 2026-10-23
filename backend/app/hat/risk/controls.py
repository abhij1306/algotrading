"""The control plane's database operations: kill switches, pause, mode changes, the exposure
ladder and the nightly bot snapshot (HAT-RISK-040/060..065, HAT-GOV-081/084).

Every function changes state and appends its event in the caller's open transaction through
`events.record`, so the two commit or roll back together. None commits. The rules they apply are
the pure ones in `kill.py`, `exposure.py` and `modes.py`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..engine import journal
from ..engine.lifecycle import AWAITING_APPROVAL
from ..errors import HatError
from ..events import record
from ..models import (
    HatAllocationDaily,
    HatApproval,
    HatBot,
    HatDailyBotSnapshot,
    HatExposureState,
    HatFill,
    HatKillSwitch,
    HatModeChange,
    HatOrder,
    HatPosition,
    HatSignal,
    HatTradeResult,
)
from ..ops.jobs import JobResult
from ..settings import load_settings
from ..time import IST, as_utc, trade_date_of, utc_now
from . import kill
from .exposure import ClosedTrade, LadderState, apply_trade
from .modes import ModeChange, plan_mode_change
from .policy import RiskPolicy
from .sessions import SessionCalendar

SNAPSHOT_AT = time(15, 30)  # the close: a snapshot is read as taken at the session close (IST)
ALERT_KILL_DRAWDOWN = "kill.drawdown"  # a drawdown kill tripped: the scope is manage-only
ALERT_FALLBACK_MARKS = "drawdown.fallback_marks"  # holdings marked without a fresh price
# An instrument's latest trade and whether it is fresh; (None, False) when none is known.
Marks = Callable[[uuid.UUID], tuple[Decimal | None, bool]]

logger = logging.getLogger(__name__)


class ControlError(HatError):
    """A control operation was refused; the message names the rule, never a payload."""


def _aggregate(scope: str, bot_id: uuid.UUID | None) -> uuid.UUID:
    """The event aggregate of a kill: the bot, or for the account the tenant's id."""
    return bot_id if scope == "bot" and bot_id is not None else load_settings().user_id


def _actor_payload(actor: str, chat_id: int | None, payload: dict) -> dict:
    return {**payload, "chat_id": chat_id} if actor == "user" else payload


# --- Kill switches ----------------------------------------------------------------------------


def kill_rows(session: Session) -> list[kill.KillRow]:
    rows = session.scalars(select(HatKillSwitch)).all()
    return [
        kill.KillRow(r.scope, r.bot_id, r.state, r.tripped_at, r.reason, r.rearmed_at) for r in rows
    ]


def kill_state(session: Session, bot_id: uuid.UUID) -> kill.KillState:
    """The switches bearing on `bot_id` now, for the entry gate."""
    bot = session.get(HatBot, bot_id)
    paused = bot is None or bot.status != "active"  # an unknown or non-active bot takes no entry
    return kill.kill_state_of(kill_rows(session), bot_id, paused=paused)


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
                "signal.expired",
                {"reason": reason},
                aggregate_type="signal",
                aggregate_id=signal.id,
                now=now,
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
    if kill.current_state(kill_rows(session), scope, bot_id, stream=stream) == kill.TRIPPED:
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
        payload=_actor_payload(actor, chat_id, {"scope": scope, "reason": reason}),
        bot_id=bot_id,
        occurred_at=now,
    )
    if channel == "risk":
        cancel_pending_approvals(session, bot_id if scope == "bot" else None, now, "kill")
    return row


def _latest_trip(
    session: Session, scope: str, bot_id: uuid.UUID | None, stream: str
) -> HatKillSwitch | None:
    rows = [
        r
        for r in session.scalars(
            select(HatKillSwitch).where(
                HatKillSwitch.scope == scope, HatKillSwitch.bot_id == bot_id
            )
        )
        if kill.stream_of(r.reason) == stream
    ]
    tripped = [r for r in rows if r.state == kill.TRIPPED]
    return max(tripped, key=lambda r: as_utc(r.tripped_at)) if tripped else None


def _arm(
    session: Session,
    scope: str,
    bot_id: uuid.UUID | None,
    stream: str,
    now: datetime,
    actor: str,
    chat_id: int | None,
) -> HatKillSwitch | None:
    if kill.current_state(kill_rows(session), scope, bot_id, stream=stream) != kill.TRIPPED:
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
        payload=_actor_payload(actor, chat_id, {"scope": scope, "reason": trip.reason}),
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


# --- Drawdown kills ---------------------------------------------------------------------------


def _snapshot_instant(trade_date: date) -> datetime:
    return datetime.combine(trade_date, SNAPSHOT_AT, tzinfo=IST)


def _last_rearm(session: Session, scope: str, bot_id: uuid.UUID | None) -> datetime | None:
    rows = [
        r.rearmed_at
        for r in session.scalars(
            select(HatKillSwitch).where(
                HatKillSwitch.scope == scope,
                HatKillSwitch.bot_id == bot_id,
                HatKillSwitch.state == kill.ARMED,
            )
        )
        if kill.channel_of(r.reason) == "risk" and r.rearmed_at is not None
    ]
    return max(rows, key=as_utc) if rows else None


def _capital_by_snapshot(
    session: Session, snaps: Sequence[HatDailyBotSnapshot]
) -> dict[tuple[uuid.UUID, date], Decimal]:
    """The allocation capital each snapshot's equity was measured against, so a planned
    reallocation can be told from a loss (HAT-RISK-066): its equity less the realised P&L of the
    real trades closed by it and the marked P&L of those still open (`open_pnl`). Not the
    allocation row of its date: the snapshot runs on its own time (PR 33), so that row may be
    missing or have been written after it. A snapshot with no allocation of its bot on or before
    its date has no known capital (left out)."""
    bots = {s.bot_id for s in snaps}
    first = dict(
        session.execute(
            select(HatAllocationDaily.bot_id, func.min(HatAllocationDaily.trade_date))
            .where(HatAllocationDaily.bot_id.in_(bots))
            .group_by(HatAllocationDaily.bot_id)
        ).all()
    )
    closed: dict[uuid.UUID, list[tuple[datetime, Decimal]]] = {}
    for bot_id, closed_at, pnl in session.execute(
        select(HatPosition.bot_id, HatPosition.closed_at, HatTradeResult.realized_pnl)
        .join(HatTradeResult, HatTradeResult.position_id == HatPosition.id)
        .where(
            HatPosition.bot_id.in_(bots),
            HatPosition.is_virtual.is_(False),
            HatPosition.closed_at.is_not(None),
        )
    ):
        closed.setdefault(bot_id, []).append((as_utc(closed_at), Decimal(pnl)))
    out = {}
    for s in snaps:
        if s.bot_id not in first or first[s.bot_id] > s.trade_date:
            continue
        cut = _snapshot_instant(s.trade_date)
        realized = sum((p for at, p in closed.get(s.bot_id, []) if at <= cut), Decimal(0))
        out[(s.bot_id, s.trade_date)] = Decimal(s.equity) - realized - Decimal(s.open_pnl or 0)
    return out


def evaluate_drawdown_kills(
    session: Session,
    policy: RiskPolicy,
    now: datetime,
    live: Mapping[uuid.UUID, tuple[Decimal, Decimal]] | None = None,
) -> list[str]:
    """Trip the account and bot kills from `hat_daily_bot_snapshot` (HAT-RISK-060/061): a
    switch trips when a snapshot after its last re-arm is at or past its drawdown threshold.
    `live` (each bot's (equity, capital) marked at `now`, `live_equity`) adds a point at `now`
    to each series, so a drawdown trips during the session on the same thresholds (P5, X9).
    Returns the scopes tripped now (`account`, or a bot id).

    Equity is allocation capital plus realised P&L plus the open positions marked to market, so
    a cut in a sleeve's capital by the allocator is not a loss. The bot series carries peaks that
    `snapshot_bots` already restated for reallocation; the account series is restated here from
    the allocation history (`kill.adjusted_series`). A snapshot with zero allocated capital is not
    measured: nothing was at risk (HAT-RISK-066).
    """
    snaps = session.scalars(select(HatDailyBotSnapshot)).all()
    capital = _capital_by_snapshot(session, snaps)
    tripped: list[str] = []
    for bot_id, series in _bot_series(snaps, capital, now, live or {}).items():
        if kill.tripped_since(
            series, policy.bot_drawdown_pct, _last_rearm(session, "bot", bot_id)
        ) and trip_kill(session, scope="bot", bot_id=bot_id, reason="drawdown_bot", now=now):
            tripped.append(str(bot_id))
    account = _account_series(snaps, capital, now, live or {})
    if kill.tripped_since(
        account, policy.account_drawdown_pct, _last_rearm(session, "account", None)
    ) and trip_kill(session, scope="account", bot_id=None, reason="drawdown_account", now=now):
        tripped.append("account")
    return tripped


def _bot_series(
    snaps: Sequence[HatDailyBotSnapshot],
    capital: Mapping[tuple[uuid.UUID, date], Decimal],
    now: datetime,
    live: Mapping[uuid.UUID, tuple[Decimal, Decimal]],
) -> dict[uuid.UUID, list[kill.EquityPoint]]:
    """Each bot's snapshot points with their stored peaks, then its point marked at `now`."""
    by_bot: dict[uuid.UUID, list[kill.EquityPoint]] = {}
    last: dict[uuid.UUID, kill.EquityPoint] = {}
    last_capital: dict[uuid.UUID, Decimal | None] = {}
    for s in sorted(snaps, key=lambda row: row.trade_date):
        point = kill.EquityPoint(
            _snapshot_instant(s.trade_date), Decimal(s.equity), Decimal(s.equity_peak)
        )
        last[s.bot_id] = point
        last_capital[s.bot_id] = capital.get((s.bot_id, s.trade_date))
        if last_capital[s.bot_id] != 0:
            by_bot.setdefault(s.bot_id, []).append(point)
    for bot_id, (equity, cap) in live.items():
        if cap != 0:
            point = kill.live_point(last.get(bot_id), last_capital.get(bot_id), now, equity, cap)
            by_bot.setdefault(bot_id, []).append(point)
    return by_bot


def _account_series(
    snaps: Sequence[HatDailyBotSnapshot],
    capital: Mapping[tuple[uuid.UUID, date], Decimal],
    now: datetime,
    live: Mapping[uuid.UUID, tuple[Decimal, Decimal]],
) -> list[kill.EquityPoint]:
    """The account's points: the bots' snapshots summed per date with peaks restated for
    reallocation (`kill.adjusted_series`), then the sum of the bots marked at `now`."""
    totals: dict[date, tuple[Decimal, Decimal | None]] = {}
    for s in snaps:
        equity, cap = totals.get(s.trade_date, (Decimal(0), Decimal(0)))
        known = capital.get((s.bot_id, s.trade_date))
        totals[s.trade_date] = (
            equity + Decimal(s.equity),
            None if cap is None or known is None else cap + known,
        )
    account = kill.adjusted_series(
        kill.SnapshotPoint(_snapshot_instant(day), equity, cap)
        for day, (equity, cap) in totals.items()
    )
    live_capital = sum((cap for _, cap in live.values()), Decimal(0))
    if live_capital != 0:
        previous = account[-1] if account else None
        known = None if previous is None else totals[previous.at.date()][1]
        equity = sum((equity for equity, _ in live.values()), Decimal(0))
        account.append(kill.live_point(previous, known, now, equity, live_capital))
    return account


# --- Pause and resume -------------------------------------------------------------------------


def pause_bot(
    session: Session,
    bot_id: uuid.UUID,
    now: datetime,
    *,
    actor: str = "user",
    chat_id: int | None = None,
) -> bool:
    """Pause an active bot: manage-only at once, `bot.paused` in the same transaction.
    False when it was not active."""
    bot = session.get(HatBot, bot_id)
    if bot is None or bot.status != "active":
        return False
    bot.status = "paused"
    record(
        session,
        aggregate_type="bot",
        aggregate_id=bot_id,
        event_type="bot.paused",
        actor=actor,
        payload=_actor_payload(actor, chat_id, {}),
        bot_id=bot_id,
        occurred_at=now,
    )
    return True


def resume_bot(
    session: Session,
    bot_id: uuid.UUID,
    now: datetime,
    *,
    actor: str = "user",
    chat_id: int | None = None,
) -> bool:
    """Resume a paused bot (`bot.resumed`). It never lifts a kill switch: the kills are rows of
    their own, and `kill_state` still reads them. False when it was not paused."""
    bot = session.get(HatBot, bot_id)
    if bot is None or bot.status != "paused":
        return False
    bot.status = "active"
    record(
        session,
        aggregate_type="bot",
        aggregate_id=bot_id,
        event_type="bot.resumed",
        actor=actor,
        payload=_actor_payload(actor, chat_id, {}),
        bot_id=bot_id,
        occurred_at=now,
    )
    return True


# --- Mode changes -----------------------------------------------------------------------------


def queue_mode_change(
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
    """Queue a mode change for the next session open (`mode.change_requested`). Raises
    `ModeChangeError` inside market hours, for an unknown mode, or without a known next open."""
    bot = session.get(HatBot, bot_id)
    if bot is None:
        raise ControlError("unknown bot")
    if via not in ("telegram", "dashboard"):
        raise ControlError("a mode change comes through telegram or the dashboard")
    plan: ModeChange = plan_mode_change(to_mode, requested_at, calendar)
    row = HatModeChange(
        bot_id=bot_id,
        from_mode=bot.execution_mode,
        to_mode=plan.to_mode,
        requested_at=plan.requested_at,
        effective_from=plan.effective_from,
        via=via,
    )
    session.add(row)
    session.flush()
    record(
        session,
        aggregate_type="bot",
        aggregate_id=bot_id,
        event_type="mode.change_requested",
        actor=actor,
        payload=_actor_payload(
            actor, chat_id, {"to_mode": to_mode, "effective_from": plan.effective_from.isoformat()}
        ),
        bot_id=bot_id,
        occurred_at=requested_at,
    )
    return row


# --- The exposure ladder ----------------------------------------------------------------------


def record_closed_trade(
    session: Session, bot_id: uuid.UUID, r_multiple: Decimal, is_virtual: bool, policy: RiskPolicy
) -> LadderState:
    """Move the bot's ladder for one closed trade, in the caller's transaction (the lifecycle
    calls this where it writes `hat_trade_result`). A virtual trade changes nothing."""
    row = session.get(HatExposureState, bot_id)
    if row is None:
        row = HatExposureState(bot_id=bot_id, level="L1", consecutive_wins=0, consecutive_losses=0)
        session.add(row)
    state = LadderState(row.level, row.consecutive_wins, row.consecutive_losses)
    after = apply_trade(state, ClosedTrade(r_multiple, is_virtual), policy)
    if after != state:
        row.level = after.level
        row.consecutive_wins = after.consecutive_wins
        row.consecutive_losses = after.consecutive_losses
        row.updated_at = utc_now()
    return after


def ladder_state(session: Session, bot_id: uuid.UUID) -> LadderState:
    row = session.get(HatExposureState, bot_id)
    if row is None:
        return LadderState()
    return LadderState(row.level, row.consecutive_wins, row.consecutive_losses)


# --- The nightly snapshot ---------------------------------------------------------------------


def _realized(session: Session, bot_id: uuid.UUID, cut: datetime) -> Decimal:
    """The bot's cumulative realised P&L of real trades closed by `cut`."""
    return sum(
        (
            Decimal(r.realized_pnl)
            for r in session.scalars(
                select(HatTradeResult)
                .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
                .where(
                    HatPosition.bot_id == bot_id,
                    HatPosition.is_virtual.is_(False),
                    HatPosition.closed_at <= cut,
                )
            )
        ),
        Decimal(0),
    )


def _holdings(session: Session, bot_id: uuid.UUID, marks: Marks) -> list[kill.Holding]:
    """The bot's open real positions as `kill.open_pnl` marks them: the net cash of each one's
    fills (`write_result` sums the same fills once it closes), its stop and its latest trade."""
    out = []
    for position in session.scalars(
        select(HatPosition).where(
            HatPosition.bot_id == bot_id,
            HatPosition.is_virtual.is_(False),
            HatPosition.closed_at.is_(None),
        )
    ):
        cash = Decimal(0)
        for side, qty, price, fee in session.execute(
            select(HatOrder.side, HatFill.qty, HatFill.price, HatFill.fees)
            .join(HatOrder, HatOrder.id == HatFill.order_id)
            .where(HatOrder.signal_id == position.signal_id)
        ):
            cash += (1 if side == "sell" else -1) * qty * Decimal(price) - Decimal(fee)
        price, fresh = marks(position.instrument_id)
        out.append(
            kill.Holding(position.qty_open, cash, Decimal(position.current_stop), price, fresh)
        )
    return out


def _allocation_on(session: Session, bot_id: uuid.UUID, day: date) -> HatAllocationDaily | None:
    """The bot's latest allocation on or before `day` (the capital in force)."""
    return session.scalar(
        select(HatAllocationDaily)
        .where(HatAllocationDaily.bot_id == bot_id, HatAllocationDaily.trade_date <= day)
        .order_by(HatAllocationDaily.trade_date.desc())
        .limit(1)
    )


def live_equity(
    session: Session, now: datetime, marks: Marks
) -> tuple[dict[uuid.UUID, tuple[Decimal, Decimal]], int]:
    """Each allocated bot's equity marked at `now` (capital in force + realised P&L so far +
    its open positions at `marks`, `kill.open_pnl`) with its capital, for
    `evaluate_drawdown_kills(live=...)`; and how many holdings were marked by a fallback (no
    fresh trade: the lower of the last trade and the stop)."""
    out: dict[uuid.UUID, tuple[Decimal, Decimal]] = {}
    fallbacks = 0
    for bot in session.scalars(select(HatBot)).all():
        allocation = _allocation_on(session, bot.id, trade_date_of(now))
        if allocation is None:
            continue
        pnl, missed = kill.open_pnl(_holdings(session, bot.id, marks))
        fallbacks += missed
        capital = Decimal(allocation.capital)
        out[bot.id] = (capital + _realized(session, bot.id, now) + pnl, capital)
    return out, fallbacks


def _snapshot_peak(
    session: Session, bot_id: uuid.UUID, trade_date: date, equity: Decimal, capital: Decimal
) -> Decimal:
    """The running peak at `trade_date`: the previous snapshot's peak restated for any change of
    allocation capital since (`kill.rebase_peak`), or, when the bot has no earlier snapshot, its
    capital (a new sleeve starts there, so a first day's loss is measured; `kill.live_point`).
    Rounded up to the stored 4 places, so rounding never hides a drawdown.

    The previous capital is the one that snapshot used (its equity less its P&L), not
    an allocation row read now: the snapshot runs on its own time (PR 33), so an `allocate` that
    committed after it, or a night with no allocation of its own, would give another capital."""
    previous = session.scalars(
        select(HatDailyBotSnapshot)
        .where(HatDailyBotSnapshot.bot_id == bot_id, HatDailyBotSnapshot.trade_date < trade_date)
        .order_by(HatDailyBotSnapshot.trade_date.desc())
        .limit(1)
    ).first()
    if previous is None:
        return max(equity, capital)
    before = _capital_by_snapshot(session, [previous]).get((bot_id, previous.trade_date))
    base = kill.rebase_peak(
        Decimal(previous.equity), Decimal(previous.equity_peak), before, capital
    )
    return max(equity, base.quantize(Decimal("0.0001"), rounding=ROUND_CEILING))


def _no_marks(_instrument_id: uuid.UUID) -> tuple[Decimal | None, bool]:
    return None, False


def snapshot_bots(
    session: Session, trade_date: date, marks: Marks = _no_marks
) -> tuple[JobResult, int]:
    """`snapshot.bots` (HAT-OPS): write one `hat_daily_bot_snapshot` per bot for `trade_date`.
    Returns the result and how many holdings were marked by a fallback.

    Equity = the sleeve's allocation capital + its cumulative realised P&L + its open real
    positions marked at `marks` (the day's official closes; P5, X9: `open_pnl`, stored); the
    peak is the largest equity seen so far, restated for any change of allocation capital so a
    planned reallocation never reads as a loss (HAT-RISK-066, `_snapshot_peak`); open risk is the
    sum of (entry - stop) x qty over its open real positions. Idempotent: a rerun for the
    same date rewrites the same row, so a crash after the body and before the success is
    recorded does no harm. The capital is the bot's latest allocation on or before the date
    (PR 33): the job runs on its own time, not after `allocate`, so a night whose quality gate
    failed (no allocation for the date) still measures the drawdown kills on the capital in
    force. Bots never allocated are skipped.
    """
    rows = 0
    fallbacks = 0
    for bot in session.scalars(select(HatBot)).all():
        allocation = _allocation_on(session, bot.id, trade_date)
        if allocation is None:
            continue
        pnl, missed = kill.open_pnl(_holdings(session, bot.id, marks))
        fallbacks += missed
        realized = _realized(session, bot.id, _snapshot_instant(trade_date))
        equity = Decimal(allocation.capital) + realized + pnl
        peak = _snapshot_peak(session, bot.id, trade_date, equity, Decimal(allocation.capital))
        open_risk = sum(
            (
                max(Decimal(p.avg_entry) - Decimal(p.current_stop), Decimal(0)) * p.qty_open
                for p in session.scalars(
                    select(HatPosition).where(
                        HatPosition.bot_id == bot.id,
                        HatPosition.is_virtual.is_(False),
                        HatPosition.closed_at.is_(None),
                    )
                )
            ),
            Decimal(0),
        )
        day_start = _snapshot_instant(trade_date).replace(hour=0, minute=0)
        day_r = sum(
            (
                Decimal(r.r_multiple)
                for r in session.scalars(
                    select(HatTradeResult)
                    .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
                    .where(
                        HatPosition.bot_id == bot.id,
                        HatPosition.is_virtual.is_(False),
                        HatPosition.closed_at >= day_start,
                        HatPosition.closed_at <= _snapshot_instant(trade_date),
                    )
                )
            ),
            Decimal(0),
        )
        values = {
            "equity": equity,
            "equity_peak": peak,
            "open_pnl": pnl,
            "open_risk": open_risk,
            "realized_r": day_r,
            "exposure_level": ladder_state(session, bot.id).level,
            "currency": allocation.currency,
        }
        existing = session.get(HatDailyBotSnapshot, (bot.id, trade_date))
        if existing is None:
            session.add(HatDailyBotSnapshot(bot_id=bot.id, trade_date=trade_date, **values))
        else:
            for key, value in values.items():
                setattr(existing, key, value)
        rows += 1
    return JobResult(calls=0, rows=rows), fallbacks
