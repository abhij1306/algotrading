"""Builders shared by the portfolio properties: a market, the seed policy, bots in given lifecycle
states and regime rows (SQLite in memory)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal

from sqlalchemy.orm import Session

from app.hat.engine.portfolio import regime, service
from app.hat.models import HatBot, HatMarket, HatStrategy
from app.hat.risk.policy_service import seed_initial_policy

D = Decimal
TRADE_DAY = date(2026, 10, 21)

USER = uuid.UUID("00000000-0000-7000-8000-000000000001")


def seed_world(session: Session, bots: list[tuple[str, str, str]]) -> list[uuid.UUID]:
    """A market, the seed policy and one bot per (strategy name, lifecycle state, cluster)."""
    market = HatMarket(
        code="NSE",
        timezone="Asia/Kolkata",
        currency="INR",
        session_open=time(9, 15),
        session_close=time(15, 30),
    )
    session.add(market)
    session.flush()
    seed_initial_policy(session, USER, datetime(2026, 1, 1, tzinfo=UTC))
    ids = []
    for name, state, cluster in bots:
        strategy = HatStrategy(user_id=USER, market_id=market.id, name=name, persona="trader")
        session.add(strategy)
        session.flush()
        bot = HatBot(
            id=uuid.uuid4(),
            user_id=USER,
            strategy_id=strategy.id,
            broker_account_id=uuid.uuid4(),
            execution_mode="shadow",
            status="active",
            lifecycle_state=state,
            risk_cluster=cluster,
        )
        session.add(bot)
        ids.append(bot.id)
    session.flush()
    return ids


def inputs_for(state_close: str) -> regime.RegimeInputs:
    """Inputs the classifier maps to `state_close` (a state name)."""
    table = {
        regime.BULL_QUIET: (D(110), D(105), D(100), D(10), D(60)),
        regime.BULL_VOLATILE: (D(110), D(105), D(100), D(80), D(60)),
        regime.CORRECTION: (D(104), D(105), D(100), D(10), D(60)),
        regime.BEAR: (D(99), D(105), D(100), D(10), D(60)),
    }
    close, fast, slow, vix_pct, breadth = table[state_close]
    return regime.RegimeInputs(close, fast, slow, D(15), vix_pct, breadth)


def put_regime(session: Session, state: str, day: date = TRADE_DAY) -> None:
    market_id = service.market_id_of(session)
    model = service.ensure_regime_model(session, market_id)
    service.record_regime(session, day, inputs_for(state), market_id=market_id, model=model)
