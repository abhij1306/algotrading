"""The day loop's private ledger (HAT-BT-010): an in-memory SQLite database holding the run's one
bot, its pinned strategy version, its universe (point in time) and the virtual signals, orders,
fills, positions and trade results the engine writes. The ledger is the shadow engine's own
schema; it never touches the HAT database and nothing in it reaches a broker. At the end of the
run the loop reads back what the engine wrote: the watchlist, trade and fill rows, and the cash
flows, summed exactly."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import time
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...base import Base
from ..models import (
    HatBot,
    HatFill,
    HatIndustry,
    HatMarket,
    HatOrder,
    HatPosition,
    HatSignal,
    HatStrategy,
    HatStrategyVersion,
    HatTradeResult,
    HatUniverse,
    HatUniverseMember,
    HatUniverseVersion,
    HatWatchlistEntry,
)
from ..time import as_utc, trade_date_of
from .data import MarketData, classifications
from .report import FillRow, TradeRow, WatchRow
from .spec import RunSpec

__all__ = ["exact_flows", "fill_rows", "ledger", "trade_rows", "watch_rows"]

_SANDBOX_USER = uuid.UUID(int=1)
_ZERO = Decimal(0)


def ledger(spec: RunSpec, data: MarketData) -> tuple[Session, uuid.UUID]:
    """A private in-memory database seeded with the run's bot, version and universe."""
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    market = HatMarket(
        code="NSE",
        timezone="Asia/Kolkata",
        currency="INR",
        session_open=time(9, 15),
        session_close=time(15, 30),
    )
    session.add(market)
    session.flush()
    strategy = HatStrategy(
        user_id=_SANDBOX_USER, market_id=market.id, name=spec.config.bot, persona="trader"
    )
    universe = HatUniverse(user_id=_SANDBOX_USER, market_id=market.id, name="run", kind="manual")
    session.add_all([strategy, universe])
    session.flush()
    version = HatStrategyVersion(
        id=spec.strategy_version_id,
        strategy_id=strategy.id,
        version=1,
        status="shadow",
        config=spec.config.to_json(),
        config_hash=spec.config.hash(),
    )
    universe_version = HatUniverseVersion(
        universe_id=universe.id, version=1, status="live", definition={}, config_hash="run"
    )
    session.add_all([version, universe_version])
    session.flush()
    for interval in data.members:
        session.add(
            HatUniverseMember(
                universe_id=universe.id,
                instrument_id=interval.instrument_id,
                effective_from=interval.effective_from,
                effective_to=interval.effective_to,
                reason="constituent",
            )
        )
    for instrument_id in sorted({str(i.instrument_id) for i in data.members}):
        # the live lookup reads the classification in force on the trade date
        for effective_from, industry in classifications(data, instrument_id):
            session.add(
                HatIndustry(
                    instrument_id=uuid.UUID(instrument_id),
                    sector="",
                    industry=industry,
                    effective_from=effective_from,
                )
            )
    bot = HatBot(
        id=uuid.uuid4(),
        user_id=_SANDBOX_USER,
        strategy_id=strategy.id,
        live_version_id=version.id,
        broker_account_id=uuid.UUID(int=2),
        execution_mode="shadow",
        status="active",
        universe_version_id=universe_version.id,
        lifecycle_state="research",
        risk_cluster="run",
    )
    session.add(bot)
    session.flush()
    return session, bot.id


# --- what the run wrote -----------------------------------------------------------------------


def exact_flows(session: Session) -> list[tuple[str, Decimal, Decimal]]:
    """Per side, the traded value and fees of every fill, summed exactly in Decimal."""
    totals: dict[str, tuple[Decimal, Decimal]] = {}
    for side, qty, price, fees in session.execute(
        select(HatOrder.side, HatFill.qty, HatFill.price, HatFill.fees).join(
            HatOrder, HatOrder.id == HatFill.order_id
        )
    ):
        value, fee = totals.get(side, (_ZERO, _ZERO))
        totals[side] = (value + qty * Decimal(price), fee + Decimal(fees))
    return [(side, value, fee) for side, (value, fee) in sorted(totals.items())]


def watch_rows(session: Session, bot_id: uuid.UUID) -> tuple[WatchRow, ...]:
    rows = [
        WatchRow(
            trade_date=r.trade_date,
            playbook=r.playbook,
            instrument_id=str(r.instrument_id),
            score=Decimal(r.score),
            pivot=None if r.pivot is None else Decimal(r.pivot),
            planned_stop=None if r.planned_stop is None else Decimal(r.planned_stop),
        )
        for r in session.scalars(
            select(HatWatchlistEntry).where(HatWatchlistEntry.bot_id == bot_id)
        )
    ]
    rows.sort(key=lambda w: (w.trade_date, -w.score, w.playbook, w.instrument_id))
    return tuple(rows)


def trade_rows(
    session: Session, bot_id: uuid.UUID, watched: Sequence[WatchRow]
) -> tuple[TradeRow, ...]:
    rows = []
    query = (
        select(HatPosition, HatTradeResult, HatSignal)
        .join(HatTradeResult, HatTradeResult.position_id == HatPosition.id)
        .join(HatSignal, HatSignal.id == HatPosition.signal_id)
        .where(HatPosition.bot_id == bot_id)
    )
    # the watchlist rows in their sorted order, so a repeated key resolves the same each run
    scores = {(w.playbook, w.instrument_id, w.trade_date): w.score for w in watched}
    for position, result, signal in session.execute(query).all():
        qty = session.scalar(
            select(func.sum(HatFill.qty))
            .join(HatOrder, HatOrder.id == HatFill.order_id)
            .where(HatOrder.signal_id == signal.id, HatOrder.purpose == "entry")
        )
        rows.append(
            TradeRow(
                entry_date=signal.trade_date,
                exit_date=trade_date_of(as_utc(position.closed_at)),
                playbook=signal.playbook,
                instrument_id=str(signal.instrument_id),
                qty=int(qty or 0),
                entry_price=Decimal(position.avg_entry),
                initial_stop=Decimal(position.initial_stop),
                exit_reason=result.exit_reason,
                realized_pnl=Decimal(result.realized_pnl),
                fees=Decimal(result.fees),
                r_multiple=Decimal(result.r_multiple),
                mfe_r=Decimal(result.mfe_r),
                mae_r=Decimal(result.mae_r),
                holding_days=result.holding_days,
                strategy_version_id=signal.strategy_version_id,
                risk_policy_version_id=signal.risk_policy_version_id,
                score=scores.get((signal.playbook, str(signal.instrument_id), signal.trade_date)),
            )
        )
    rows.sort(key=lambda t: (t.exit_date, t.entry_date, t.instrument_id, t.playbook))
    return tuple(rows)


def fill_rows(session: Session) -> tuple[FillRow, ...]:
    rows = [
        FillRow(
            day=trade_date_of(as_utc(fill.filled_at)),
            instrument_id=str(signal.instrument_id),
            purpose=order.purpose,
            side=order.side,
            qty=fill.qty,
            price=Decimal(fill.price),
            fees=Decimal(fill.fees),
        )
        for fill, order, signal in session.execute(
            select(HatFill, HatOrder, HatSignal)
            .join(HatOrder, HatOrder.id == HatFill.order_id)
            .join(HatSignal, HatSignal.id == HatOrder.signal_id)
        ).all()
    ]
    rows.sort(key=lambda f: (f.day, f.instrument_id, f.purpose, f.side, f.qty, f.price))
    return tuple(rows)
