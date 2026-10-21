"""A bot with a pinned strategy version, a universe and a policy, on SQLite in memory, and
helpers that drive the shadow engine (`app.hat.engine.trades`) by trigger and by price."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import yaml
from hat_risk_support import AT, TRADE_DAY, valid_ctx, version_of
from sqlalchemy.orm import Session

from app.hat.engine import exits, trades
from app.hat.engine.strategy.config import parse_config
from app.hat.engine.strategy.versions import seed_text
from app.hat.models import (
    HatBot,
    HatIndustry,
    HatMarket,
    HatStrategy,
    HatStrategyVersion,
    HatUniverse,
    HatUniverseMember,
    HatUniverseVersion,
)
from app.hat.risk.policy import seed_policy
from app.hat.risk.policy_service import seed_initial_policy

__all__ = [
    "AT",
    "TRADE_DAY",
    "Trade",
    "World",
    "exit_stack",
    "open_trade",
    "seed_engine_world",
    "step_trade",
]

D = Decimal
USER = uuid.UUID("00000000-0000-7000-8000-000000000011")
SEED = "stage2-contraction-breakout"

_STACKS: dict[str, list[dict[str, Any]]] = {
    "trend": [
        {"rule": "hard_stop"},
        {"rule": "move_stop_to_breakeven", "at_r": 1},
        {"rule": "close_below_sma", "sma": 10, "sell_fraction": 1.0},
        {"rule": "time_stop", "days": 3, "min_r": 1},
    ],
    "partials": [
        {"rule": "hard_stop"},
        {"rule": "partial_at_r", "at_r": 1, "sell_fraction": 0.33},
        {"rule": "move_stop_to_breakeven", "after": "partial"},
        {"rule": "partial_at_r", "at_r": 2, "sell_fraction": 0.5},
        {"rule": "pct_gain_target", "pct": 35, "sell_fraction": 1.0},
    ],
    "stop_only": [{"rule": "hard_stop"}],
}


def exit_stack(kind: str) -> list[dict[str, Any]]:
    return [dict(rule) for rule in _STACKS[kind]]


@dataclass
class World:
    user_id: uuid.UUID
    bot_id: uuid.UUID
    strategy_id: uuid.UUID
    version_id: uuid.UUID
    universe_id: uuid.UUID
    instruments: list[uuid.UUID]
    policy_version_id: uuid.UUID  # the stored version 1, so a signal's FK points at a real row


@dataclass
class Trade:
    world: World
    outcome: trades.EntryOutcome
    opened: datetime
    high: Decimal = Decimal(100)  # the running extremes since the entry at 100 (MFE, MAE)
    low: Decimal = Decimal(100)


def seed_engine_world(
    session: Session,
    *,
    exits: list[dict[str, Any]] | None = None,
    instruments: int = 4,
    mode: str = "shadow",
    member_from: date | None = None,
) -> World:
    market = HatMarket(
        code="NSE",
        timezone="Asia/Kolkata",
        currency="INR",
        session_open=time(9, 15),
        session_close=time(15, 30),
    )
    session.add(market)
    session.flush()
    policy = seed_initial_policy(session, USER, datetime(2026, 1, 1, tzinfo=UTC))
    config = yaml.safe_load(seed_text(SEED))
    if exits is not None:
        config["exits"] = exits
    parse_config(config)
    strategy = HatStrategy(
        user_id=USER, market_id=market.id, name=f"s-{uuid.uuid4()}", persona="trader"
    )
    session.add(strategy)
    session.flush()
    version = HatStrategyVersion(
        id=uuid.uuid4(),
        strategy_id=strategy.id,
        version=1,
        status="shadow",
        config=config,
        config_hash="h",
    )
    universe = HatUniverse(user_id=USER, market_id=market.id, name="u", kind="manual")
    session.add_all([version, universe])
    session.flush()
    uversion = HatUniverseVersion(
        universe_id=universe.id, version=1, status="live", definition={}, config_hash="h"
    )
    session.add(uversion)
    session.flush()
    ids = [uuid.uuid4() for _ in range(instruments)]
    for n, instrument in enumerate(ids):
        session.add(
            HatUniverseMember(
                universe_id=universe.id,
                instrument_id=instrument,
                effective_from=member_from or TRADE_DAY - timedelta(days=30),
                reason="constituent",
            )
        )
        session.add(
            HatIndustry(
                instrument_id=instrument,
                sector="s",
                industry=f"industry-{n}",
                effective_from=TRADE_DAY - timedelta(days=400),
            )
        )
    bot = HatBot(
        id=uuid.uuid4(),
        user_id=USER,
        strategy_id=strategy.id,
        live_version_id=version.id,
        broker_account_id=uuid.uuid4(),
        execution_mode=mode,
        status="active",
        universe_version_id=uversion.id,
        lifecycle_state="live",
        risk_cluster="c1",
    )
    session.add(bot)
    session.flush()
    return World(USER, bot.id, strategy.id, version.id, universe.id, ids, policy.id)


def request_for(
    world: World, instrument: uuid.UUID, playbook: str = "pb", **ctx: object
) -> trades.EntryRequest:
    stored = replace(version_of(seed_policy()), version_id=world.policy_version_id)
    ctx.setdefault("policy_versions", [stored])
    context = valid_ctx(**ctx)
    return trades.EntryRequest(
        bot_id=world.bot_id,
        playbook=playbook,
        instrument_id=instrument,
        trade_date=TRADE_DAY,
        triggered_at=AT,
        context=context,
    )


def open_trade(
    session: Session,
    world: World,
    instrument: uuid.UUID | None = None,
    playbook: str = "pb",
    **ctx: object,
) -> Trade:
    outcome = trades.open_entry(
        session, request_for(world, instrument or world.instruments[0], playbook, **ctx)
    )
    return Trade(world, outcome, AT)


def step_trade(
    session: Session,
    trade: Trade,
    price: Decimal,
    *,
    days: int = 0,
    minutes: int = 0,
    phase: exits.Phase = "close",
) -> exits.Evaluation:
    """One price of the position at `days` days and `minutes` minutes after the entry."""
    assert trade.outcome.position_id is not None
    trade.high, trade.low = max(trade.high, price), min(trade.low, price)
    view = exits.ExitView(price=price, days_held=days, sma={10: D(100)})
    return trades.evaluate_position(
        session,
        trade.outcome.position_id,
        view,
        phase,
        trade.opened + timedelta(days=days, minutes=minutes + 1),
        high=trade.high,
        low=trade.low,
    )
