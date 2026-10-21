"""The portfolio layer's database operations (05): universes, the regime row, the sleeve lifecycle
and the allocation rows.

Every function changes state and appends its event in the caller's open transaction through
`events.record`, so the two commit or roll back together (HAT-SCH-062). None commits. The rules
they apply are the pure ones in this package.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.confighash import config_hash
from app.hat.errors import HatError
from app.hat.events import record
from app.hat.models import (
    HatAllocationDaily,
    HatBot,
    HatDailyBotSnapshot,
    HatEvent,
    HatMarket,
    HatPosition,
    HatRegimeModelVersion,
    HatRegimeState,
    HatSignal,
    HatStrategy,
    HatTradeResult,
    HatTradingCalendar,
    HatUniverse,
    HatUniverseMember,
    HatUniverseVersion,
)
from app.hat.risk.policy import RiskPolicy, effective_version
from app.hat.risk.policy_service import load_versions
from app.hat.time import IST, as_utc, trade_date_of, utc_now

from . import lifecycle, regime, universe
from .allocator import SleeveInput, allocate, sleeve_key
from .health import Health, health_of

EVENT_LIFECYCLE = "sleeve.lifecycle_changed"
EVENT_REGIME = "regime.state_recorded"
EVENT_ALLOCATION = "allocation.computed"
ALLOCATION_AT = time(15, 30)  # the policy version in force at the close of the trade date


class PortfolioError(HatError):
    """A portfolio operation was refused; `code` names the rule and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- Universes (HAT-PORT-020..022) ------------------------------------------------------------


def create_universe(
    session: Session, *, user_id: uuid.UUID, market_id: uuid.UUID, name: str, kind: str = "manual"
) -> HatUniverse:
    row = HatUniverse(user_id=user_id, market_id=market_id, name=name, kind=kind)
    session.add(row)
    session.flush()
    return row


def membership_rows(session: Session, universe_id: uuid.UUID) -> list[universe.Interval]:
    return [
        universe.Interval(m.instrument_id, m.effective_from, m.effective_to)
        for m in session.scalars(
            select(HatUniverseMember).where(HatUniverseMember.universe_id == universe_id)
        )
    ]


def members_of(session: Session, universe_id: uuid.UUID, day: date) -> frozenset[uuid.UUID]:
    """The members of `universe_id` on `day` (point in time, HAT-PORT-021)."""
    return universe.members_on(membership_rows(session, universe_id), day)


def apply_membership(
    session: Session,
    universe_id: uuid.UUID,
    target: Collection[uuid.UUID],
    day: date,
    *,
    reasons: dict[uuid.UUID, str] | None = None,
) -> tuple[int, int]:
    """Make the members on `day` equal `target`: new intervals for additions, a closed interval
    for removals. Returns (added, removed). A removal never touches an open position: leaving
    the universe stops entries only (HAT-PORT-023)."""
    rows = membership_rows(session, universe_id)
    to_add, to_remove = universe.plan_sync(rows, target, day)
    for instrument_id in sorted(to_add):
        universe.add_member(rows, instrument_id, day)  # refuses an edit into recorded history
        session.add(
            HatUniverseMember(
                universe_id=universe_id,
                instrument_id=instrument_id,
                effective_from=day,
                reason=(reasons or {}).get(instrument_id, "constituent"),
            )
        )
    for instrument_id in sorted(to_remove):
        universe.remove_member(rows, instrument_id, day)
        member = session.scalars(
            select(HatUniverseMember).where(
                HatUniverseMember.universe_id == universe_id,
                HatUniverseMember.instrument_id == instrument_id,
                HatUniverseMember.effective_to.is_(None),
            )
        ).one()
        member.effective_to = day
    session.flush()
    return len(to_add), len(to_remove)


def seed_universe_version(
    session: Session,
    universe_id: uuid.UUID,
    *,
    constituents: Collection[uuid.UUID],
    include: Collection[uuid.UUID] = (),
    exclude: Collection[uuid.UUID] = (),
    as_of: date,
) -> HatUniverseVersion:
    """A new draft version of the MVP universe: the index constituents plus the includes minus
    the excludes as of `as_of` (HAT-PORT-022). The draft only records its member list: the
    universe's membership intervals change when the version is published, so a draft is never
    seen by `members_of`. Each edit of the lists is a new version (HAT-PORT-020)."""
    target = universe.effective_members(constituents, include, exclude)
    reasons = sorted(str(i) for i in include if i not in set(constituents))
    previous = session.scalars(
        select(HatUniverseVersion.version).where(HatUniverseVersion.universe_id == universe_id)
    ).all()
    definition = {
        "kind": "manual",
        "source": "index_constituents",
        "as_of": as_of.isoformat(),
        "include": sorted(str(i) for i in include),
        "exclude": sorted(str(i) for i in exclude),
        "members": len(target),
        "member_ids": sorted(str(i) for i in target),
        "included_by_owner": reasons,
    }
    row = HatUniverseVersion(
        universe_id=universe_id,
        version=max(previous, default=0) + 1,
        status="draft",
        definition=definition,
        config_hash=config_hash(definition),
    )
    session.add(row)
    session.flush()
    return row


def publish_version(session: Session, version_id: uuid.UUID, now: datetime) -> HatUniverseVersion:
    """Make a universe version live and retire the one that was (one live version per universe,
    HAT-PORT-020). The old one is retired first, so the partial unique index never sees two. The
    version's member list becomes the universe's membership from its `as_of` date (history before
    it is never touched)."""
    version = session.get(HatUniverseVersion, version_id)
    if version is None:
        raise PortfolioError("universe_version_unknown")
    for other in session.scalars(
        select(HatUniverseVersion).where(
            HatUniverseVersion.universe_id == version.universe_id,
            HatUniverseVersion.status == "live",
            HatUniverseVersion.id != version.id,
        )
    ):
        other.status = "retired"
    session.flush()
    definition = version.definition
    if "member_ids" in definition:
        owner_included = set(definition.get("included_by_owner", ()))
        apply_membership(
            session,
            version.universe_id,
            [uuid.UUID(i) for i in definition["member_ids"]],
            date.fromisoformat(definition["as_of"]),
            reasons={uuid.UUID(i): "include" for i in owner_included},
        )
    version.status = "live"
    version.promoted_at = now
    session.flush()
    return version


# --- Regime (HAT-PORT-030..034) ---------------------------------------------------------------


def market_id_of(session: Session, code: str = "NSE") -> uuid.UUID:
    found = session.scalar(select(HatMarket.id).where(HatMarket.code == code))
    if found is None:
        raise PortfolioError("market_unknown")
    return found


def ensure_regime_model(session: Session, market_id: uuid.UUID) -> HatRegimeModelVersion:
    """The rules model, version 1, of the market; created on first use (HAT-PORT-034)."""
    row = session.scalars(
        select(HatRegimeModelVersion).where(
            HatRegimeModelVersion.market_id == market_id,
            HatRegimeModelVersion.kind == "rules",
            HatRegimeModelVersion.version == 1,
        )
    ).first()
    if row is None:
        row = HatRegimeModelVersion(
            market_id=market_id,
            kind="rules",
            version=1,
            status="live",
            config=regime.rules_v1_config(),
        )
        session.add(row)
        session.flush()
    return row


def _confirmation_of(row: HatRegimeState) -> regime.Confirmation:
    data = row.inputs
    return regime.Confirmation(
        str(data["confirmed_state"]), str(data["raw_state"]), int(data["run"])
    )


def regime_row(
    session: Session, market_id: uuid.UUID, model_id: uuid.UUID, day: date
) -> HatRegimeState | None:
    return session.get(HatRegimeState, (day, market_id, model_id))


def latest_regime_before(
    session: Session, market_id: uuid.UUID, model_id: uuid.UUID, day: date
) -> HatRegimeState | None:
    return session.scalars(
        select(HatRegimeState)
        .where(
            HatRegimeState.market_id == market_id,
            HatRegimeState.model_version_id == model_id,
            HatRegimeState.trade_date < day,
        )
        .order_by(HatRegimeState.trade_date.desc())
        .limit(1)
    ).first()


def record_regime(
    session: Session,
    day: date,
    inputs: regime.RegimeInputs,
    *,
    market_id: uuid.UUID,
    model: HatRegimeModelVersion,
) -> HatRegimeState:
    """Store the regime of `day`: the raw state, the inputs and the confirmation (`confirmed` is
    true when the raw state is the confirmed one). One row per (market, model, day): a rerun
    keeps the first (HAT-PORT-034)."""
    existing = regime_row(session, market_id, model.id, day)
    if existing is not None:
        return existing
    before = latest_regime_before(session, market_id, model.id, day)
    raw = regime.classify_inputs(inputs)
    state = regime.confirm_step(None if before is None else _confirmation_of(before), raw)
    row = HatRegimeState(
        trade_date=day,
        market_id=market_id,
        model_version_id=model.id,
        state=raw,
        probabilities=None,
        inputs={**inputs.to_json(), **state.to_json()},
        confirmed=raw == state.confirmed,
    )
    session.add(row)
    session.flush()
    record(
        session,
        aggregate_type="regime",
        aggregate_id=model.id,
        event_type=EVENT_REGIME,
        actor="scheduler",
        payload={
            "trade_date": day.isoformat(),
            "raw_state": raw,
            "confirmed_state": state.confirmed,
        },
        occurred_at=_close_of(day),
    )
    return row


def confirmed_state(
    session: Session, market_id: uuid.UUID, model_id: uuid.UUID, day: date
) -> str | None:
    """The confirmed state stored for `day`; None when the regime job has not run for it."""
    row = regime_row(session, market_id, model_id, day)
    return None if row is None else _confirmation_of(row).confirmed


def regime_table(model: HatRegimeModelVersion) -> dict[str, Decimal]:
    """The momentum-sleeve multiplier table of the model version (stored with it); the defaults
    when the version holds none."""
    stored = (model.config or {}).get("multipliers", {}).get("momentum")
    if not stored:
        return dict(regime.DEFAULT_MULTIPLIERS)
    return {state: Decimal(str(stored[state])) for state in regime.STATES}


# --- The sleeve lifecycle (HAT-PORT-060..065) -------------------------------------------------


def _close_of(day: date) -> datetime:
    return datetime.combine(day, ALLOCATION_AT, tzinfo=IST)


def trading_sessions(session: Session, market_id: uuid.UUID | None = None) -> frozenset[date]:
    query = select(HatTradingCalendar.day).where(HatTradingCalendar.is_trading.is_(True))
    if market_id is not None:
        query = query.where(HatTradingCalendar.market_id == market_id)
    return frozenset(session.scalars(query))


def stage_entered_on(session: Session, bot_id: uuid.UUID, stage: str) -> date | None:
    """The trade date the sleeve last entered `stage`, from its lifecycle events."""
    events = session.scalars(
        select(HatEvent)
        .where(HatEvent.aggregate_id == bot_id, HatEvent.event_type == EVENT_LIFECYCLE)
        .order_by(HatEvent.id.desc())
    )
    for event in events:
        if event.payload.get("to") == stage:
            return trade_date_of(as_utc(event.occurred_at))
    return None


def trade_results(
    session: Session,
    bot_id: uuid.UUID,
    *,
    virtual: bool,
    since: datetime | None = None,
    until: datetime | None = None,
) -> tuple[Decimal, ...]:
    """The R multiples of the sleeve's closed real (or shadow) trades closed from `since` to
    `until` (both inclusive, either open), oldest first. A replay of an earlier trade date passes
    its own close as `until`, so later trades never change an earlier decision. The veto trades
    of skipped confirm signals (virtual, but not shadow trades) are in neither: they feed the
    veto score only (HAT-EXEC-130)."""
    query = (
        select(HatTradeResult.r_multiple, HatPosition.closed_at)
        .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
        .outerjoin(HatSignal, HatSignal.id == HatPosition.signal_id)
        .where(HatPosition.bot_id == bot_id, HatPosition.is_virtual.is_(virtual))
        .where(HatPosition.is_virtual.is_(False) | (HatSignal.mode == "shadow"))
        .order_by(HatPosition.closed_at)
    )
    rows = session.execute(query).all()
    return tuple(
        Decimal(r)
        for r, closed in rows
        if (since is None or (closed is not None and as_utc(closed) >= as_utc(since)))
        and (until is None or (closed is not None and as_utc(closed) <= as_utc(until)))
    )


def latest_snapshot(
    session: Session, bot_id: uuid.UUID, before: date
) -> HatDailyBotSnapshot | None:
    return session.scalars(
        select(HatDailyBotSnapshot)
        .where(HatDailyBotSnapshot.bot_id == bot_id, HatDailyBotSnapshot.trade_date < before)
        .order_by(HatDailyBotSnapshot.trade_date.desc())
        .limit(1)
    ).first()


def sleeve_health(
    session: Session, bot_id: uuid.UUID, trade_date: date, policy: RiskPolicy
) -> Health:
    """The health multiplier from the sleeve's latest snapshot before `trade_date`; the full
    value when it has none yet (a new sleeve has no drawdown)."""
    snap = latest_snapshot(session, bot_id, trade_date)
    if snap is None:
        return Health(policy.health_full_mult, False)
    return health_of(Decimal(snap.equity), Decimal(snap.equity_peak), policy)


BacktestEvidence = Callable[[Session, HatBot], lifecycle.BacktestFacts]


def no_backtest_evidence(_session: Session, _bot: HatBot) -> lifecycle.BacktestFacts:
    """No evidence: Research -> Backtested stays closed. The backtest's source is
    `app.hat.backtest.runs.backtest_evidence` (HAT-BT-060)."""
    return lifecycle.BacktestFacts()


def load_evidence(
    session: Session,
    bot: HatBot,
    trade_date: date,
    policy: RiskPolicy,
    backtest: BacktestEvidence = no_backtest_evidence,
) -> lifecycle.Evidence:
    """Everything the guards read for `bot` on `trade_date`."""
    facts = backtest(session, bot)
    entered = stage_entered_on(session, bot.id, lifecycle.PROBATION)
    health_zero = False
    if bot.lifecycle_state == lifecycle.LIVE:
        health_zero = sleeve_health(session, bot.id, trade_date, policy).probation
    return lifecycle.Evidence(
        today=trade_date,
        dsr_prob=facts.dsr_prob,
        backtest_trades=facts.trades,
        backtest_trials=facts.trials,
        sessions=trading_sessions(session),
        shadow_started_on=stage_entered_on(session, bot.id, lifecycle.SHADOW),
        health_zero=health_zero,
        live_r=trade_results(session, bot.id, virtual=False, until=_close_of(trade_date)),
        probation_entered_on=entered,
        probation_shadow_r=trade_results(
            session,
            bot.id,
            virtual=True,
            since=None if entered is None else _close_of(entered),
            until=_close_of(trade_date),
        ),
        backtest_expectancy=facts.expectancy,
    )


def transition(
    session: Session,
    bot_id: uuid.UUID,
    target: str,
    evidence: lifecycle.Evidence,
    *,
    actor: str = "engine",
    chat_id: int | None = None,
    by_owner: bool = False,
    now: datetime | None = None,
) -> HatBot:
    """Move a sleeve along one allowed edge and append `sleeve.lifecycle_changed` in the same
    transaction. Raises `LifecycleError` (changing nothing) when the edge or its guard is not
    met. This is the only code that writes `hat_bot.lifecycle_state` (CONF
    `lifecycle_single_writer`)."""
    bot = session.get(HatBot, bot_id)
    if bot is None:
        raise PortfolioError("bot_unknown")
    verdict = lifecycle.guard(bot.lifecycle_state, target, evidence, by_owner=by_owner)
    if not verdict.allowed:
        raise lifecycle.LifecycleError(verdict.reason)
    if by_owner and actor != "user":
        raise lifecycle.LifecycleError("owner_actor")
    before = bot.lifecycle_state
    bot.lifecycle_state = target
    payload: dict[str, object] = {"from": before, "to": target, "reason": verdict.reason}
    if actor == "user":
        payload["chat_id"] = chat_id
    record(
        session,
        aggregate_type="bot",
        aggregate_id=bot_id,
        event_type=EVENT_LIFECYCLE,
        actor=actor,
        payload=payload,
        bot_id=bot_id,
        occurred_at=now or utc_now(),
    )
    session.flush()
    return bot


def advance_automatic(
    session: Session,
    trade_date: date,
    policy: RiskPolicy,
    backtest: BacktestEvidence = no_backtest_evidence,
) -> list[tuple[uuid.UUID, str]]:
    """The edges the engine takes by itself: Live -> Probation when the trigger fires, and
    Probation -> Retired when its gate opens. Returns (bot, new state) for each move. Safe to
    repeat: a sleeve already moved no longer matches."""
    moved: list[tuple[uuid.UUID, str]] = []
    now = _close_of(trade_date)
    for bot in session.scalars(select(HatBot).order_by(HatBot.id)).all():
        target = {lifecycle.LIVE: lifecycle.PROBATION, lifecycle.PROBATION: lifecycle.RETIRED}.get(
            bot.lifecycle_state
        )
        if target is None:
            continue
        evidence = load_evidence(session, bot, trade_date, policy, backtest)
        if lifecycle.guard(bot.lifecycle_state, target, evidence).allowed:
            transition(session, bot.id, target, evidence, now=now)
            moved.append((bot.id, target))
    return moved


# --- The allocation (HAT-PORT-050..056) -------------------------------------------------------


def policy_at(session: Session, user_id: uuid.UUID, trade_date: date) -> RiskPolicy:
    """The risk policy in force at the close of `trade_date`: the only source of the bounds,
    base weights, cluster cap and risk per trade an allocation uses (HAT-PORT-051)."""
    version = effective_version(load_versions(session, user_id), _close_of(trade_date))
    if version is None:
        raise PortfolioError("allocate_no_policy")
    return version.policy


def previous_allocation(
    session: Session, bot_id: uuid.UUID, trade_date: date
) -> HatAllocationDaily | None:
    return session.scalars(
        select(HatAllocationDaily)
        .where(HatAllocationDaily.bot_id == bot_id, HatAllocationDaily.trade_date < trade_date)
        .order_by(HatAllocationDaily.trade_date.desc())
        .limit(1)
    ).first()


@dataclass(frozen=True)
class AllocationReport:
    rows: int
    moved: tuple[tuple[uuid.UUID, str], ...]


def run_allocation(
    session: Session,
    trade_date: date,
    *,
    user_id: uuid.UUID,
    total_equity: Decimal,
    currency: str = "INR",
    market_code: str = "NSE",
    backtest: BacktestEvidence = no_backtest_evidence,
) -> AllocationReport:
    """The nightly allocation: after the regime row of `trade_date` exists, move the sleeves the
    engine moves (a health 0 sleeve into Probation, HAT-PORT-040), compute the weights and write
    one `hat_allocation_daily` row per sleeve. A rerun keeps the rows already written. Raises
    `PortfolioError` when there is no regime row or no policy: no weight is made on a guess.
    Open positions are not touched (HAT-PORT-053)."""
    market_id = market_id_of(session, market_code)
    model = ensure_regime_model(session, market_id)
    state = confirmed_state(session, market_id, model.id, trade_date)
    if state is None:
        raise PortfolioError("allocate_no_regime")
    policy = policy_at(session, user_id, trade_date)
    moved = advance_automatic(session, trade_date, policy, backtest)
    table = regime_table(model)
    sleeves = []
    bots = session.scalars(select(HatBot).order_by(HatBot.id)).all()
    for bot in bots:
        strategy = session.get(HatStrategy, bot.strategy_id)
        before = previous_allocation(session, bot.id, trade_date)
        sleeves.append(
            SleeveInput(
                bot_id=bot.id,
                sleeve=sleeve_key(strategy.name if strategy else ""),
                cluster=bot.risk_cluster,
                lifecycle_state=bot.lifecycle_state,
                regime_table_value=regime.multiplier_for(state, table),
                health=sleeve_health(session, bot.id, trade_date, policy),
                previous_regime_mult=None if before is None else Decimal(before.regime_mult),
                previous_health_mult=None if before is None else Decimal(before.health_mult),
            )
        )
    written = 0
    for allocation in allocate(sleeves, policy, total_equity):
        if session.get(HatAllocationDaily, (trade_date, allocation.bot_id)) is not None:
            continue
        session.add(
            HatAllocationDaily(
                trade_date=trade_date,
                bot_id=allocation.bot_id,
                base_weight=allocation.base_weight,
                regime_mult=allocation.regime_mult,
                health_mult=allocation.health_mult,
                final_weight=allocation.final_weight,
                capital=allocation.capital,
                risk_pct=allocation.risk_pct,
                currency=currency,
            )
        )
        record(
            session,
            aggregate_type="bot",
            aggregate_id=allocation.bot_id,
            event_type=EVENT_ALLOCATION,
            actor="scheduler",
            payload={
                "trade_date": trade_date.isoformat(),
                "base_weight": str(allocation.base_weight),
                "regime_mult": str(allocation.regime_mult),
                "health_mult": str(allocation.health_mult),
                "final_weight": str(allocation.final_weight),
            },
            bot_id=allocation.bot_id,
            occurred_at=_close_of(trade_date),
        )
        written += 1
    session.flush()
    return AllocationReport(written, tuple(moved))
