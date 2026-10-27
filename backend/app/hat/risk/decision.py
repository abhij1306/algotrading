"""The HAT risk decision: every check of 06 composed into one total function (HAT-RISK-100/101/102).

`decide` is what `app.hat.risk.service.evaluate_signal` calls; the session comes from the
context's trading calendar. It is the only place that calls
`size_position`, so no other module sizes a HAT order (CONF `risk.single_sizing_path`). It never
raises: a missing, non-finite or float input, or any arithmetic failure, is a REJECT, never a
default (fail closed).

An exit or a protective-stop move is always accepted: no cap, kill, pause or exposure level can
trap a position (HAT-RISK-035). Anything that is not exactly one of those actions is an entry
and is checked in full, so an action name cannot be used to skip a check.

The order of the entry checks is fixed, and the first failing one names the reason.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from ..time import trade_date_of
from . import caps
from .exposure import LadderState, risk_multiplier
from .kill import KillState
from .policy import PolicyError, PolicyVersion, RiskPolicy, effective_version
from .sessions import SessionCalendar, SessionState
from .sizing import MissingInputError, SizingInputError, SizingInputs, exact_input, size_position

logger = logging.getLogger(__name__)

ENTRY = "entry"
EXIT_ACTIONS = frozenset({"exit", "stop_move"})


class Reason(StrEnum):
    """Every reason an entry is rejected (HAT-RISK-101)."""

    MISSING_INPUT = "missing_input"
    INVALID_INPUT = "invalid_input"
    KILL_ACCOUNT = "kill_account"
    KILL_BOT = "kill_bot"
    KILL_HEALTH = "kill_health"
    BOT_PAUSED = "bot_paused"
    MARKET_CLOSED = "market_closed"
    STALE_FEED = "stale_feed"
    QUALITY_FAILED = "quality_failed"
    REGIME_GATE = "regime_gate"
    STOP_NOT_BELOW_ENTRY = "stop_not_below_entry"
    MAX_CHASE = "max_chase"
    ADD_TO_LOSER = "add_to_loser"
    POSITION_EXISTS = "position_exists"
    MAX_POSITIONS = "max_positions"
    INDUSTRY_CAP = "industry_cap"
    ENTRY_CAP = "entry_cap"
    STOPOUT_CAP = "stopout_cap"
    COOLDOWN = "cooldown"
    NO_ALLOCATION = "no_allocation"
    ALLOCATION_EXCEEDS_POLICY = "allocation_exceeds_policy"
    SIZE_ZERO_RISK = "size_zero_risk"
    SIZE_ZERO_POSITION = "size_zero_position"
    SIZE_ZERO_LIQUIDITY = "size_zero_liquidity"
    SIZE_ZERO_CASH = "size_zero_cash"
    SIZE_BELOW_MIN = "size_below_min"
    GLOBAL_OPEN_RISK = "global_open_risk"


REASON_CODES = frozenset(reason.value for reason in Reason)


@dataclass(frozen=True)
class SignalContext:
    """Everything the risk decision reads about one signal. A field left None is a missing input
    and makes an entry a REJECT with reason `missing_input`, except where noted."""

    signal_id: UUID | None = None
    bot_id: object | None = None
    action: str = ENTRY
    strategy_key: str | None = None
    at: datetime | None = None
    instrument_id: object | None = None
    industry: str | None = None
    is_virtual: bool | None = None
    entry_price: Decimal | None = None
    stop_price: Decimal | None = None
    trigger_level: Decimal | None = None
    locked_upper_circuit: bool | None = None
    # The playbook's last configured max-chase percent; None when none was ever configured (valid).
    configured_max_chase_pct: Decimal | None = None
    equity: Decimal | None = None
    # hat_allocation_daily.risk_pct, which already holds regime_mult x health_mult.
    allocation_risk_pct: Decimal | None = None
    regime_mult: Decimal | None = None  # recorded for audit only, never applied again
    health_mult: Decimal | None = None  # recorded for audit only, never applied again
    free_cash: Decimal | None = None
    adv20: Decimal | None = None
    total_equity: Decimal | None = None
    policy_versions: Sequence[PolicyVersion] | None = None
    ladder: LadderState | None = None
    open_positions: Sequence[caps.OpenPosition] | None = None
    entries_today: int | None = None
    stopouts_today: int | None = None
    # The trade dates on which the bot closed this instrument by `hard_stop` (empty: never). Read
    # only while the policy's `cooldown_sessions` is above 0, when None is a missing input.
    hard_stop_days: Sequence[date] | None = None
    gate_on: bool | None = None
    feed_stale: bool | None = None
    quality_failed: bool | None = None
    kill: KillState | None = None
    calendar: SessionCalendar | None = None


@dataclass(frozen=True)
class RiskDecision:
    """ACCEPT with a quantity, stop and risk amount, or REJECT with a reason code."""

    accepted: bool
    reason: str | None = None
    quantity: int | None = None
    stop: Decimal | None = None
    risk_amount: Decimal | None = None
    warnings: tuple[str, ...] = ()
    policy_version_id: UUID | None = None
    binding: str | None = None
    risk_pct_used: Decimal | None = None
    regime_mult: Decimal | None = None
    health_mult: Decimal | None = None

    def to_json(self) -> dict[str, Any]:
        """The document stored in `hat_signal.risk_decision`."""
        return {
            "decision": "ACCEPT" if self.accepted else "REJECT",
            "reason": self.reason,
            "qty": self.quantity,
            "stop": None if self.stop is None else str(self.stop),
            "risk_amount": None if self.risk_amount is None else str(self.risk_amount),
            "warnings": list(self.warnings),
            "policy_version_id": None
            if self.policy_version_id is None
            else str(self.policy_version_id),
            "binding": self.binding,
            "risk_pct_used": None if self.risk_pct_used is None else str(self.risk_pct_used),
            "regime_mult": None if self.regime_mult is None else str(self.regime_mult),
            "health_mult": None if self.health_mult is None else str(self.health_mult),
        }


def _reject(
    reason: Reason, *, version: UUID | None = None, warnings: Sequence[str] = ()
) -> RiskDecision:
    return RiskDecision(
        accepted=False, reason=reason.value, policy_version_id=version, warnings=tuple(warnings)
    )


def _exit_decision(ctx: SignalContext) -> RiskDecision:
    """An exit or a stop move: always accepted, with a warning for each limit that would have
    applied to an entry. Reading the context never changes the outcome."""
    warnings = []
    try:
        kill = ctx.kill
        if isinstance(kill, KillState) and kill.manage_only:
            warnings.append("manage_only_exit_allowed")
        calendar, at = ctx.calendar, ctx.at
        if isinstance(calendar, SessionCalendar) and isinstance(at, datetime):
            if calendar.state(at) is not SessionState.OPEN:
                warnings.append("exit_outside_session")
    except Exception:  # noqa: BLE001 - an unreadable context must never stop an exit
        warnings.append("context_unreadable")
    return RiskDecision(accepted=True, warnings=tuple(warnings))


def _require(value: object, name: str) -> Any:  # noqa: ANN401 - returns the value it was given
    if value is None:
        raise MissingInputError(f"{name}: missing")
    return value


def _flag(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise MissingInputError(f"{name}: missing")
    return value


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MissingInputError(f"{name}: missing")
    return value


@dataclass(frozen=True)
class _Entry:
    """The context of an entry, read and validated once: every field is present and of its type."""

    at: datetime
    strategy_key: str
    version: PolicyVersion
    entry: Decimal
    stop: Decimal
    trigger: Decimal
    equity: Decimal
    total_equity: Decimal
    alloc: Decimal
    cash: Decimal
    adv20: Decimal
    kill: KillState
    ladder: LadderState
    positions: Sequence[caps.OpenPosition]
    industry: str
    bot_id: object
    instrument_id: object
    virtual: bool
    stale: bool
    quality_failed: bool
    gate_on: bool
    locked: bool
    entries_today: int
    stopouts_today: int
    last_hard_stop: date | None
    calendar: SessionCalendar
    session: SessionState
    configured_chase: Decimal | None
    p_max: Decimal


def _last_hard_stop(ctx: SignalContext, version: PolicyVersion, at: datetime) -> date | None:
    """The bot's latest hard-stop day of the instrument at or before the signal's trade date;
    None when there is none or the policy has no cooldown (the input is then not read)."""
    if version.policy.cooldown_sessions == 0:
        return None
    days = _require(ctx.hard_stop_days, "hard_stop_days")
    if isinstance(days, (str, bytes)) or not isinstance(days, Sequence):
        raise MissingInputError("hard_stop_days: not a sequence")
    if any(isinstance(d, datetime) or not isinstance(d, date) for d in days):
        raise MissingInputError("hard_stop_days: not trade dates")
    today = trade_date_of(at)
    return max((d for d in days if d <= today), default=None)


def _read_entry(ctx: SignalContext) -> _Entry:
    """Every input of an entry, or `MissingInputError` for the first missing or inexact one."""
    at = _require(ctx.at, "at")
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise MissingInputError("at: needs an aware instant")
    strategy_key = _require(ctx.strategy_key, "strategy_key")
    version = effective_version(_require(ctx.policy_versions, "policy_versions"), at)
    if version is None:
        raise MissingInputError("policy: no version in force")
    entry = exact_input(ctx.entry_price, "entry_price")
    stop = exact_input(ctx.stop_price, "stop_price")
    trigger = exact_input(ctx.trigger_level, "trigger_level")
    equity = exact_input(ctx.equity, "equity")
    total_equity = exact_input(ctx.total_equity, "total_equity")
    alloc = exact_input(ctx.allocation_risk_pct, "allocation_risk_pct")
    cash = exact_input(ctx.free_cash, "free_cash", cash=False)
    adv20 = exact_input(ctx.adv20, "adv20")
    kill = _require(ctx.kill, "kill")
    ladder = _require(ctx.ladder, "ladder")
    positions = _require(ctx.open_positions, "open_positions")
    calendar = _require(ctx.calendar, "calendar")
    industry = _require(ctx.industry, "industry")
    bot_id = _require(ctx.bot_id, "bot_id")
    instrument_id = _require(ctx.instrument_id, "instrument_id")
    flags = {
        name: _flag(getattr(ctx, name), name)
        for name in (
            "is_virtual",
            "feed_stale",
            "quality_failed",
            "gate_on",
            "locked_upper_circuit",
        )
    }
    entries_today = _count(ctx.entries_today, "entries_today")
    stopouts_today = _count(ctx.stopouts_today, "stopouts_today")
    if not isinstance(kill, KillState) or not isinstance(calendar, SessionCalendar):
        raise MissingInputError("kill or calendar: wrong type")
    session = calendar.state(at)
    if session is SessionState.UNKNOWN:
        raise MissingInputError("calendar: no row for the trade date")
    chase = ctx.configured_max_chase_pct
    p_max = version.policy.p_max.get(strategy_key)
    if p_max is None or ladder.level not in version.policy.ladder:
        raise MissingInputError("policy: no entry for this strategy or level")
    return _Entry(
        at=at,
        strategy_key=strategy_key,
        version=version,
        entry=entry,
        stop=stop,
        trigger=trigger,
        equity=equity,
        total_equity=total_equity,
        alloc=alloc,
        cash=cash,
        adv20=adv20,
        kill=kill,
        ladder=ladder,
        positions=positions,
        industry=industry,
        bot_id=bot_id,
        instrument_id=instrument_id,
        virtual=flags["is_virtual"],
        stale=flags["feed_stale"],
        quality_failed=flags["quality_failed"],
        gate_on=flags["gate_on"],
        locked=flags["locked_upper_circuit"],
        entries_today=entries_today,
        stopouts_today=stopouts_today,
        last_hard_stop=_last_hard_stop(ctx, version, at),
        calendar=calendar,
        session=session,
        configured_chase=None if chase is None else exact_input(chase, "configured_max_chase_pct"),
        p_max=p_max,
    )


def _state_reason(e: _Entry) -> Reason | None:
    """The first of the kill, pause, market and data checks that fails."""
    checks = (
        (e.kill.account_tripped, Reason.KILL_ACCOUNT),
        (e.kill.bot_tripped, Reason.KILL_BOT),
        (e.kill.health_tripped, Reason.KILL_HEALTH),
        (e.kill.paused, Reason.BOT_PAUSED),
        (e.session is not SessionState.OPEN, Reason.MARKET_CLOSED),
        (e.stale, Reason.STALE_FEED),
        (e.quality_failed, Reason.QUALITY_FAILED),
        (not caps.regime_gate_open(e.gate_on), Reason.REGIME_GATE),
        (not caps.stop_valid(e.entry, e.stop), Reason.STOP_NOT_BELOW_ENTRY),
    )
    for failed, reason in checks:
        if failed:
            return reason
    chase = caps.effective_max_chase_pct(e.configured_chase, e.strategy_key)
    if not caps.chase_ok(e.entry, e.trigger, chase, locked_upper=e.locked):
        return Reason.MAX_CHASE
    return None


def _cap_reason(e: _Entry) -> Reason | None:
    """The first of the position, industry and daily caps and the cooldown that fails. The
    positions are the caller's snapshot: two evaluations racing on one snapshot are serialised
    per bot by the caller (the lifecycle's per-bot transaction), not here."""
    policy = e.version.policy
    mine = [p for p in e.positions if p.is_virtual == e.virtual and p.bot_id == e.bot_id]
    held = [p for p in mine if p.instrument_id == e.instrument_id]
    checks = (
        (any(caps.is_loser(p) for p in held), Reason.ADD_TO_LOSER),
        (bool(held), Reason.POSITION_EXISTS),
        (not caps.positions_ok(len(mine), e.ladder.level, policy), Reason.MAX_POSITIONS),
        (
            not caps.industry_ok(sum(1 for p in mine if p.industry == e.industry), policy),
            Reason.INDUSTRY_CAP,
        ),
        (not caps.entries_ok(e.entries_today, policy), Reason.ENTRY_CAP),
        (not caps.stopouts_ok(e.stopouts_today, policy), Reason.STOPOUT_CAP),
        (
            not caps.cooldown_ok(e.calendar, e.last_hard_stop, trade_date_of(e.at), policy),
            Reason.COOLDOWN,
        ),
    )
    for failed, reason in checks:
        if failed:
            return reason
    return None


def _entry_decision(ctx: SignalContext) -> RiskDecision:
    e = _read_entry(ctx)
    policy: RiskPolicy = e.version.policy
    keep = e.version.version_id

    def reject(reason: Reason) -> RiskDecision:
        return _reject(reason, version=keep)

    reason = _state_reason(e) or _cap_reason(e)
    if reason is not None:
        return reject(reason)
    if e.alloc <= 0:
        return reject(Reason.NO_ALLOCATION)
    if e.alloc > policy.risk_pct * policy.multiplier_hi * policy.multiplier_hi:
        return reject(Reason.ALLOCATION_EXCEEDS_POLICY)
    sized = size_position(
        SizingInputs(
            equity=e.equity,
            allocation_risk_pct=e.alloc,
            level_risk_mult=risk_multiplier(e.ladder.level, policy),
            entry_price=e.entry,
            stop_price=e.stop,
            p_max=e.p_max,
            adv20=e.adv20,
            adv_cap_pct=policy.adv_cap_pct,
            free_cash=e.cash,
            min_position_pct=policy.min_position_pct,
        )
    )
    if sized.reason is not None:
        return reject(Reason(sized.reason))
    risk_amount = (e.entry - e.stop) * sized.quantity
    same_mode = [p for p in e.positions if p.is_virtual == e.virtual]
    if not caps.global_open_risk_ok(
        (p.open_risk for p in same_mode), risk_amount, e.total_equity, policy.global_open_risk_pct
    ):
        return reject(Reason.GLOBAL_OPEN_RISK)
    warnings = () if sized.binding == "risk" else (f"size_bound_by_{sized.binding}",)
    return RiskDecision(
        accepted=True,
        quantity=sized.quantity,
        stop=e.stop,
        risk_amount=risk_amount,
        warnings=warnings,
        policy_version_id=keep,
        binding=sized.binding,
        risk_pct_used=sized.risk_pct_used,
        regime_mult=ctx.regime_mult,
        health_mult=ctx.health_mult,
    )


def decide(ctx: SignalContext) -> RiskDecision:
    """ACCEPT or REJECT for one signal; total, never raises (HAT-RISK-101)."""
    try:
        if ctx.action in EXIT_ACTIONS:
            return _exit_decision(ctx)
        return _entry_decision(ctx)
    except (MissingInputError, PolicyError):
        return _reject(Reason.MISSING_INPUT)
    except SizingInputError:
        return _reject(Reason.INVALID_INPUT)
    except (ArithmeticError, TypeError, ValueError, AttributeError, KeyError, IndexError):
        logger.exception("risk decision failed closed")
        return _reject(Reason.INVALID_INPUT)
