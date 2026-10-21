"""The control plane's database operations: kill switches, pause, mode changes, the exposure
ladder and the nightly bot snapshot (06 §4, §6, §8; HAT-RISK-040/060..065, HAT-GOV-081/084).

Every function changes state and appends its event in the caller's open transaction through
`events.record`, so the two commit or roll back together. None commits. The rules they apply are
the pure ones in `kill.py`, `exposure.py` and `modes.py`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..engine.lifecycle import AWAITING_APPROVAL, next_state
from ..errors import HatError
from ..events import record
from ..models import (
    HatAllocationDaily,
    HatApproval,
    HatBot,
    HatDailyBotSnapshot,
    HatExposureState,
    HatKillSwitch,
    HatModeChange,
    HatPosition,
    HatSignal,
    HatTradeResult,
)
from ..ops.jobs import JobResult
from ..settings import load_settings
from ..time import IST, as_utc, utc_now
from . import kill
from .exposure import ClosedTrade, LadderState, apply_trade
from .modes import ModeChange, plan_mode_change
from .policy import RiskPolicy
from .sessions import SessionCalendar

SNAPSHOT_AT = time(15, 30)  # the close: a snapshot is read as taken at the session close (IST)


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
    record `signal.expired`, so it can no longer be taken. Returns how many."""
    query = select(HatApproval, HatSignal).join(HatSignal, HatSignal.id == HatApproval.signal_id)
    query = query.where(HatApproval.decision.is_(None))
    if bot_id is not None:
        query = query.where(HatSignal.bot_id == bot_id)
    count = 0
    for approval, signal in session.execute(query).all():
        approval.decision = "expired"
        approval.decided_at = now
        if signal.state == AWAITING_APPROVAL:  # the state follows its event (HAT-EXEC-042)
            signal.state = next_state(signal.state, "signal.expired")
        record(
            session,
            aggregate_type="signal",
            aggregate_id=signal.id,
            correlation_id=signal.id,
            event_type="signal.expired",
            actor="engine",
            payload={"reason": reason},
            bot_id=signal.bot_id,
            occurred_at=now,
        )
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
    """Trip a kill switch and append `kill.tripped`; None when the key's channel is already
    tripped (a repeat adds no row). A drawdown or manual trip also ends the undecided approvals
    it covers; a health trip does not (the take re-checks the gate)."""
    if reason not in kill.REASONS or scope not in ("account", "bot"):
        raise ControlError("unknown kill scope or reason")
    if (scope == "bot") != (bot_id is not None):
        raise ControlError("a bot kill names its bot and an account kill names none")
    channel = kill.channel_of(reason)
    if kill.current_state(kill_rows(session), scope, bot_id, channel) == kill.TRIPPED:
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
    session: Session, scope: str, bot_id: uuid.UUID | None, channel: str
) -> HatKillSwitch | None:
    rows = [
        r
        for r in session.scalars(
            select(HatKillSwitch).where(
                HatKillSwitch.scope == scope, HatKillSwitch.bot_id == bot_id
            )
        )
        if kill.channel_of(r.reason) == channel
    ]
    tripped = [r for r in rows if r.state == kill.TRIPPED]
    return max(tripped, key=lambda r: as_utc(r.tripped_at)) if tripped else None


def _arm(
    session: Session,
    scope: str,
    bot_id: uuid.UUID | None,
    channel: str,
    now: datetime,
    actor: str,
    chat_id: int | None,
) -> HatKillSwitch | None:
    if kill.current_state(kill_rows(session), scope, bot_id, channel) != kill.TRIPPED:
        return None
    trip = _latest_trip(session, scope, bot_id, channel)
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
    """The data or broker health check result for a bot (or the account): trip the health kill
    while it fails, clear it by itself the moment it passes (HAT-RISK-063)."""
    if reason not in kill.HEALTH_REASONS:
        raise ControlError("a health check names data_health or broker_health")
    if failing:
        return trip_kill(session, scope=scope, bot_id=bot_id, reason=reason, now=now)
    return _arm(session, scope, bot_id, "health", now, "engine", None)


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


def _capital_by_snapshot(session: Session) -> dict[tuple[uuid.UUID, date], Decimal]:
    """The allocation capital of every (bot, trade date): what each snapshot's equity was
    measured against, so a planned reallocation can be told from a loss (HAT-RISK-066)."""
    return {
        (a.bot_id, a.trade_date): Decimal(a.capital)
        for a in session.scalars(select(HatAllocationDaily))
    }


def evaluate_drawdown_kills(session: Session, policy: RiskPolicy, now: datetime) -> list[str]:
    """Trip the account and bot kills from `hat_daily_bot_snapshot` (HAT-RISK-060/061): a
    switch trips when a snapshot after its last re-arm is at or past its drawdown threshold.
    Returns the scopes tripped now (`account`, or a bot id).

    Equity is allocation capital plus realised P&L, so a cut in a sleeve's capital by the
    allocator is not a loss. The bot series carries peaks that `snapshot_bots` already restated
    for reallocation; the account series is restated here from the allocation history
    (`kill.adjusted_series`). A snapshot with zero allocated capital is not measured: nothing
    was at risk (HAT-RISK-066).
    """
    snaps = session.scalars(select(HatDailyBotSnapshot)).all()
    capital = _capital_by_snapshot(session)
    tripped: list[str] = []
    by_bot: dict[uuid.UUID, list[kill.EquityPoint]] = {}
    for s in snaps:
        if capital.get((s.bot_id, s.trade_date)) == 0:
            continue
        by_bot.setdefault(s.bot_id, []).append(
            kill.EquityPoint(
                _snapshot_instant(s.trade_date), Decimal(s.equity), Decimal(s.equity_peak)
            )
        )
    for bot_id, series in by_bot.items():
        if kill.tripped_since(
            series, policy.bot_drawdown_pct, _last_rearm(session, "bot", bot_id)
        ) and trip_kill(session, scope="bot", bot_id=bot_id, reason="drawdown_bot", now=now):
            tripped.append(str(bot_id))
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
    if kill.tripped_since(
        account, policy.account_drawdown_pct, _last_rearm(session, "account", None)
    ) and trip_kill(session, scope="account", bot_id=None, reason="drawdown_account", now=now):
        tripped.append("account")
    return tripped


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


def _snapshot_peak(
    session: Session, bot_id: uuid.UUID, trade_date: date, equity: Decimal, capital: Decimal
) -> Decimal:
    """The running peak at `trade_date`: the previous snapshot's peak restated for any change of
    allocation capital since (`kill.rebase_peak`), or the equity itself when the bot has no
    earlier snapshot. Rounded up to the stored 4 places, so rounding never hides a drawdown."""
    previous = session.scalars(
        select(HatDailyBotSnapshot)
        .where(HatDailyBotSnapshot.bot_id == bot_id, HatDailyBotSnapshot.trade_date < trade_date)
        .order_by(HatDailyBotSnapshot.trade_date.desc())
        .limit(1)
    ).first()
    if previous is None:
        return equity
    before = session.get(HatAllocationDaily, (previous.trade_date, bot_id))
    base = kill.rebase_peak(
        Decimal(previous.equity),
        Decimal(previous.equity_peak),
        None if before is None else Decimal(before.capital),
        capital,
    )
    return max(equity, base.quantize(Decimal("0.0001"), rounding=ROUND_CEILING))


def snapshot_bots(session: Session, trade_date: date) -> JobResult:
    """`snapshot.bots` (HAT-OPS): write one `hat_daily_bot_snapshot` per bot for `trade_date`.

    Equity = the sleeve's allocation capital for the date + its cumulative realised P&L
    (06 §2: "allocation + realised P&L"); the peak is the largest equity seen so far, restated
    for any change of allocation capital so a planned reallocation never reads as a loss
    (HAT-RISK-066, `_snapshot_peak`); open risk
    is the sum of (entry - stop) x qty over its open real positions. Idempotent: a rerun for the
    same date rewrites the same row, so a crash after the body and before the success is
    recorded does no harm. Bots with no allocation row for the date are skipped.
    """
    rows = 0
    for bot in session.scalars(select(HatBot)).all():
        allocation = session.get(HatAllocationDaily, (trade_date, bot.id))
        if allocation is None:
            continue
        realized = sum(
            (
                Decimal(r.realized_pnl)
                for r in session.scalars(
                    select(HatTradeResult)
                    .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
                    .where(
                        HatPosition.bot_id == bot.id,
                        HatPosition.is_virtual.is_(False),
                        HatPosition.closed_at <= _snapshot_instant(trade_date),
                    )
                )
            ),
            Decimal(0),
        )
        equity = Decimal(allocation.capital) + realized
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
    return JobResult(calls=0, rows=rows)
