"""The HAT risk decision: every check of 06 composed into one total function (HAT-RISK-100/101/102).

`decide` is what `RiskManager.evaluate_signal` calls after it has run the existing market-hours
and price-band checks. It is the only place that calls `size_position`, so no other module sizes
a HAT order (CONF `risk.single_sizing_path`). It never raises: a missing, non-finite or float
input, or any arithmetic failure, is a REJECT, never a default (fail closed).

An exit or a protective-stop move is always accepted: no cap, kill, pause or exposure level can
trap a position (HAT-RISK-035). Anything that is not exactly one of those actions is an entry
and is checked in full, so an action name cannot be used to skip a check.

The order of the entry checks is fixed, and the first failing one names the reason.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from . import caps
from .exposure import LadderState, risk_multiplier
from .kill import KillState
from .policy import PolicyError, PolicyVersion, RiskPolicy, effective_version
from .sessions import SessionCalendar, SessionState
from .sizing import MissingInputError, SizingInputError, SizingInputs, exact, size_position

logger = logging.getLogger(__name__)

ENTRY = "entry"
EXIT_ACTIONS = frozenset({"exit", "stop_move"})


class Reason(StrEnum):
    """Every reason an entry is rejected (HAT-RISK-101)."""

    MISSING_INPUT = "missing_input"
    INVALID_INPUT = "invalid_input"
    SMARTTRADER_HALT = "smarttrader_halt"
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
    NO_ALLOCATION = "no_allocation"
    ALLOCATION_EXCEEDS_POLICY = "allocation_exceeds_policy"
    SIZE_ZERO_RISK = "size_zero_risk"
    SIZE_ZERO_POSITION = "size_zero_position"
    SIZE_ZERO_LIQUIDITY = "size_zero_liquidity"
    SIZE_ZERO_CASH = "size_zero_cash"
    GLOBAL_OPEN_RISK = "global_open_risk"
    PRICE_OUT_OF_BAND = "price_out_of_band"


REASON_CODES = frozenset(reason.value for reason in Reason)


@dataclass(frozen=True)
class LegacyChecks:
    """The outcomes of the existing `RiskManager` checks that the HAT decision composes:
    True passed, False failed, None could not be evaluated (treated as missing input)."""

    market_open: bool | None
    price_band_ok: bool | None


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
    ltp: Decimal | None = None
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
    gate_on: bool | None = None
    feed_stale: bool | None = None
    quality_failed: bool | None = None
    smarttrader_halted: bool | None = None
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


def _entry_decision(ctx: SignalContext, legacy: LegacyChecks) -> RiskDecision:
    at = _require(ctx.at, "at")
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise MissingInputError("at: needs an aware instant")
    strategy_key = _require(ctx.strategy_key, "strategy_key")
    versions = _require(ctx.policy_versions, "policy_versions")
    version = effective_version(versions, at)
    if version is None:
        raise MissingInputError("policy: no version in force")
    policy: RiskPolicy = version.policy
    entry = exact(ctx.entry_price, "entry_price")
    stop = exact(ctx.stop_price, "stop_price")
    trigger = exact(ctx.trigger_level, "trigger_level")
    exact(ctx.ltp, "ltp")
    equity = exact(ctx.equity, "equity")
    total_equity = exact(ctx.total_equity, "total_equity")
    alloc = exact(ctx.allocation_risk_pct, "allocation_risk_pct")
    cash = exact(ctx.free_cash, "free_cash", cash=False)
    adv20 = exact(ctx.adv20, "adv20")
    kill = _require(ctx.kill, "kill")
    ladder = _require(ctx.ladder, "ladder")
    positions = _require(ctx.open_positions, "open_positions")
    calendar = _require(ctx.calendar, "calendar")
    industry = _require(ctx.industry, "industry")
    bot_id = _require(ctx.bot_id, "bot_id")
    instrument_id = _require(ctx.instrument_id, "instrument_id")
    virtual = _flag(ctx.is_virtual, "is_virtual")
    halted = _flag(ctx.smarttrader_halted, "smarttrader_halted")
    stale = _flag(ctx.feed_stale, "feed_stale")
    quality_failed = _flag(ctx.quality_failed, "quality_failed")
    gate_on = _flag(ctx.gate_on, "gate_on")
    locked = _flag(ctx.locked_upper_circuit, "locked_upper_circuit")
    entries_today = _count(ctx.entries_today, "entries_today")
    stopouts_today = _count(ctx.stopouts_today, "stopouts_today")
    if not isinstance(kill, KillState) or not isinstance(calendar, SessionCalendar):
        raise MissingInputError("kill or calendar: wrong type")
    if legacy.market_open is None or legacy.price_band_ok is None:
        raise MissingInputError("an existing check could not be evaluated")
    session = calendar.state(at)
    if session is SessionState.UNKNOWN:
        raise MissingInputError("calendar: no row for the trade date")
    configured_chase = ctx.configured_max_chase_pct
    if configured_chase is not None:
        configured_chase = exact(configured_chase, "configured_max_chase_pct")
    p_max = policy.p_max.get(strategy_key)
    if p_max is None or ladder.level not in policy.ladder:
        raise MissingInputError("policy: no entry for this strategy or level")
    keep = version.version_id

    def reject(reason: Reason) -> RiskDecision:
        return _reject(reason, version=keep)

    if halted:
        return reject(Reason.SMARTTRADER_HALT)
    if kill.account_tripped:
        return reject(Reason.KILL_ACCOUNT)
    if kill.bot_tripped:
        return reject(Reason.KILL_BOT)
    if kill.health_tripped:
        return reject(Reason.KILL_HEALTH)
    if kill.paused:
        return reject(Reason.BOT_PAUSED)
    if session is not SessionState.OPEN or not legacy.market_open:
        return reject(Reason.MARKET_CLOSED)
    if stale:
        return reject(Reason.STALE_FEED)
    if quality_failed:
        return reject(Reason.QUALITY_FAILED)
    if not caps.regime_gate_open(gate_on):
        return reject(Reason.REGIME_GATE)
    if not caps.stop_valid(entry, stop):
        return reject(Reason.STOP_NOT_BELOW_ENTRY)
    chase = caps.effective_max_chase_pct(configured_chase, strategy_key)
    if not caps.chase_ok(entry, trigger, chase, locked_upper=locked):
        return reject(Reason.MAX_CHASE)

    same_mode = [p for p in positions if p.is_virtual == virtual]
    mine = [p for p in same_mode if p.bot_id == bot_id]
    held = [p for p in mine if p.instrument_id == instrument_id]
    if any(caps.is_loser(p) for p in held):
        return reject(Reason.ADD_TO_LOSER)
    if held:
        return reject(Reason.POSITION_EXISTS)
    if not caps.positions_ok(len(mine), ladder.level, policy):
        return reject(Reason.MAX_POSITIONS)
    if not caps.industry_ok(sum(1 for p in mine if p.industry == industry), policy):
        return reject(Reason.INDUSTRY_CAP)
    if not caps.entries_ok(entries_today, policy):
        return reject(Reason.ENTRY_CAP)
    if not caps.stopouts_ok(stopouts_today, policy):
        return reject(Reason.STOPOUT_CAP)

    if alloc <= 0:
        return reject(Reason.NO_ALLOCATION)
    if alloc > policy.risk_pct * policy.multiplier_hi * policy.multiplier_hi:
        return reject(Reason.ALLOCATION_EXCEEDS_POLICY)
    sized = size_position(
        SizingInputs(
            equity=equity,
            allocation_risk_pct=alloc,
            level_risk_mult=risk_multiplier(ladder.level, policy),
            entry_price=entry,
            stop_price=stop,
            p_max=p_max,
            adv20=adv20,
            adv_cap_pct=policy.adv_cap_pct,
            free_cash=cash,
        )
    )
    if sized.reason is not None:
        return reject(Reason(sized.reason))
    risk_amount = (entry - stop) * sized.quantity
    if not caps.global_open_risk_ok(
        (p.open_risk for p in same_mode), risk_amount, total_equity, policy.global_open_risk_pct
    ):
        return reject(Reason.GLOBAL_OPEN_RISK)
    if not legacy.price_band_ok:
        return reject(Reason.PRICE_OUT_OF_BAND)
    warnings = () if sized.binding == "risk" else (f"size_bound_by_{sized.binding}",)
    return RiskDecision(
        accepted=True,
        quantity=sized.quantity,
        stop=stop,
        risk_amount=risk_amount,
        warnings=warnings,
        policy_version_id=keep,
        binding=sized.binding,
        risk_pct_used=sized.risk_pct_used,
        regime_mult=ctx.regime_mult,
        health_mult=ctx.health_mult,
    )


def decide(ctx: SignalContext, legacy: LegacyChecks) -> RiskDecision:
    """ACCEPT or REJECT for one signal; total, never raises (HAT-RISK-101)."""
    try:
        if ctx.action in EXIT_ACTIONS:
            return _exit_decision(ctx)
        return _entry_decision(ctx, legacy)
    except (MissingInputError, PolicyError):
        return _reject(Reason.MISSING_INPUT)
    except SizingInputError:
        return _reject(Reason.INVALID_INPUT)
    except (ArithmeticError, TypeError, ValueError, AttributeError, KeyError, IndexError):
        logger.exception("risk decision failed closed")
        return _reject(Reason.INVALID_INPUT)
