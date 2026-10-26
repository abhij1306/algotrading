"""The nightly allocation's database operations (HAT-PORT-050..056): the policy in force, the
previous rows and one `hat_allocation_daily` row per sleeve.

Every function changes state and appends its event in the caller's open transaction through
`events.record`, so the two commit or roll back together (HAT-SCH-062). None commits. The rule
it applies is the pure one in `allocator`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.configs.records import Bot, Strategy
from app.hat.configs.unit import configs_of
from app.hat.events import record
from app.hat.models import HatAllocationDaily
from app.hat.risk.policy import RiskPolicy, effective_version
from app.hat.risk.policy_service import load_versions

from . import regime
from .allocator import SleeveInput, allocate, sleeve_key
from .common import PortfolioError, close_of, market_id_of
from .regime_service import confirmed_state, ensure_regime_model, regime_profile, regime_table
from .sleeve_service import advance_automatic, sleeve_health

EVENT_ALLOCATION = "allocation.computed"


def policy_at(session: Session, user_id: uuid.UUID, trade_date: date) -> RiskPolicy:
    """The risk policy in force at the close of `trade_date`: the only source of the bounds,
    base weights, cluster cap and risk per trade an allocation uses (HAT-PORT-051)."""
    version = effective_version(load_versions(session, user_id), close_of(trade_date))
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
    moved = advance_automatic(session, trade_date, policy)
    sleeves = []
    unit = configs_of(session)
    for bot in sorted(unit.all(Bot), key=lambda b: b.id):
        strategy = unit.get(Strategy, bot.strategy_id)
        name = strategy.name if strategy else ""
        table = regime_table(model, regime_profile(name))
        before = previous_allocation(session, bot.id, trade_date)
        sleeves.append(
            SleeveInput(
                bot_id=bot.id,
                sleeve=sleeve_key(name),
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
                virtual_capital=allocation.virtual_capital,
                virtual_risk_pct=allocation.virtual_risk_pct,
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
                **(
                    {
                        "virtual_capital": str(allocation.virtual_capital),
                        "virtual_risk_pct": str(allocation.virtual_risk_pct),
                    }
                    if allocation.virtual_capital is not None
                    else {}
                ),
            },
            bot_id=allocation.bot_id,
            occurred_at=close_of(trade_date),
        )
        written += 1
    session.flush()
    return AllocationReport(written, tuple(moved))
