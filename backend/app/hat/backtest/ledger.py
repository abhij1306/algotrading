"""The day loop's private ledger (HAT-BT-010): an in-memory SQLite database (both HAT
schemas) and config store holding the run's one bot, its pinned strategy version, its universe
(point in time) and the virtual signals, orders, fills, positions and trade results the engine
writes. The ledger is the shadow engine's own schema; it never touches the HAT
databases or config files and nothing in it reaches a broker. At the end of the
run the loop reads back what the engine wrote: the watchlist, trade and fill rows, and the cash
flows, summed exactly."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import date, time
from decimal import Decimal

from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...base import Base, StateBase
from ..configs.records import Bot, Strategy, StrategyVersion
from ..configs.store import MemoryConfigStore
from ..configs.unit import STORE, configs_of
from ..data.corporate_actions import TYPE_DIVIDEND
from ..engine.portfolio.allocator import sleeve_key
from ..governance import VersionStatus
from ..models import (
    HatCorporateAction,
    HatEvent,
    HatFill,
    HatIndustry,
    HatInstrument,
    HatMarket,
    HatOrder,
    HatPosition,
    HatSignal,
    HatTradeResult,
    HatUniverse,
    HatUniverseMember,
    HatUniverseVersion,
    HatWatchlistEntry,
)
from ..time import as_utc, day_bounds, trade_date_of
from .data import MarketData, classifications, scan_night_members
from .report import FillRow, LegRow, TradeRow, WatchRow
from .spec import RunSpec

__all__ = [
    "exact_flows",
    "fill_rows",
    "fills_on",
    "ledger",
    "set_series",
    "trade_rows",
    "watch_rows",
]

_SANDBOX_USER = uuid.UUID(int=1)
_ZERO = Decimal(0)
# before its first bar an instrument has no series, and so no Fyers symbol
_NO_SERIES = ""


def ledger(spec: RunSpec, data: MarketData) -> tuple[Session, tuple[uuid.UUID, ...]]:
    """A private in-memory database and config store seeded with the run's bot, version and
    universe, and a bot and version per co-sleeve (`RunSpec.co_sleeves`), committed so the
    engine's rollbacks keep them; the bot ids, the run's first."""
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    StateBase.metadata.create_all(engine)
    session = Session(engine, info={STORE: MemoryConfigStore()})
    market = HatMarket(
        code="NSE",
        timezone="Asia/Kolkata",
        currency="INR",
        session_open=time(9, 15),
        session_close=time(15, 30),
    )
    session.add(market)
    session.flush()
    configs = configs_of(session)
    universe = HatUniverse(user_id=_SANDBOX_USER, market_id=market.id, name="run", kind="manual")
    session.add(universe)
    session.flush()
    # the run's strategy, then each co-sleeve's: one bot each, one owner, so the live account
    # caps (`owner_slots`) count every sleeve's positions
    sleeves = [(spec.config, spec.strategy_version_id, "run")] + [
        (c.config, c.strategy_version_id, sleeve_key(c.config.bot)) for c in spec.co_sleeves
    ]
    versions: list[tuple[uuid.UUID, uuid.UUID, str]] = []
    for config, version_id, cluster in sleeves:
        strategy = Strategy(
            user_id=_SANDBOX_USER, market_id=market.id, name=config.bot, persona="trader"
        )
        configs.add(strategy)
        session.flush()
        version = StrategyVersion(
            id=version_id,
            strategy_id=strategy.id,
            version=1,
            status=VersionStatus.TRIAL,
            config=config.to_json(),
            config_hash=config.hash(),
        )
        configs.add(version)
        versions.append((strategy.id, version.id, cluster))
    universe_version = HatUniverseVersion(
        universe_id=universe.id, version=1, status="live", definition={}, config_hash="run"
    )
    session.add(universe_version)
    session.flush()
    for interval in scan_night_members(data.members, data.days):
        session.add(
            HatUniverseMember(
                universe_id=universe.id,
                instrument_id=interval.instrument_id,
                effective_from=interval.effective_from,
                effective_to=interval.effective_to,
                reason="constituent",
            )
        )
    # The live scan and entry read only active stocks trading under a Fyers series (D210): each
    # member gets a row with its ISIN, its series set point in time each night by `set_series`.
    for instrument_id in sorted({str(i.instrument_id) for i in data.members}):
        session.add(
            HatInstrument(
                id=uuid.UUID(instrument_id),
                market_id=market.id,
                isin=data.isins.get(instrument_id, instrument_id),
                series=_NO_SERIES,
                symbol=instrument_id,
                name=instrument_id,
                face_value=Decimal(1),
                active=True,
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
    _seed_dividends(session, data)
    bot_ids = []
    for strategy_id, version_id, cluster in versions:
        bot = Bot(
            id=uuid.uuid4(),
            user_id=_SANDBOX_USER,
            strategy_id=strategy_id,
            live_version_id=version_id,
            broker_account_id=uuid.UUID(int=2),
            execution_mode="shadow",
            status="active",
            universe_version_id=universe_version.id,
            lifecycle_state="research",
            risk_cluster=cluster,
        )
        configs.add(bot)
        bot_ids.append(bot.id)
    session.commit()
    return session, tuple(bot_ids)


def _seed_dividends(session: Session, data: MarketData) -> None:
    """The members' dividends as the HAT database stores them, the cash per price-basis share
    the run pays (`MarketData.dividends`), so a closed trade's result counts them as a live
    one's does (`positions.write_result`, R-DIV)."""
    members = {str(i.instrument_id) for i in data.members}
    for instrument_id in sorted(members & data.dividends.keys()):
        per_day: dict[date, Decimal] = {}
        for ex_date, cash in data.dividends[instrument_id]:
            per_day[ex_date] = per_day.get(ex_date, _ZERO) + cash
        session.add_all(
            HatCorporateAction(
                instrument_id=uuid.UUID(instrument_id),
                ex_date=ex_date,
                action_type=TYPE_DIVIDEND,
                adjustment_factor=Decimal(1),
                raw_text="",
                source="backtest",
                cash_amount=cash,
            )
            for ex_date, cash in sorted(per_day.items())
        )


def set_series(session: Session, series: Mapping[str, str]) -> None:
    """Set each ledger instrument's series to `series` (by instrument id; one without a bar yet
    has none), as the live listing refresh keeps `hat_instrument.series` current."""
    for instrument_id, current in session.execute(select(HatInstrument.id, HatInstrument.series)):
        wanted = series.get(str(instrument_id), _NO_SERIES)
        if wanted != current:
            session.execute(
                update(HatInstrument).where(HatInstrument.id == instrument_id).values(series=wanted)
            )


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


def _legs(
    session: Session, signal: HatSignal, position: HatPosition, closing: str
) -> tuple[LegRow, ...]:
    """The trade's sales in order, each with the rule that sold it: a partial sale's from its
    `exit.partial` event, in order; the sale that closed the position (and any other) the
    trade's `closing` reason."""
    sales = session.execute(
        select(HatFill.filled_at, HatFill.qty, HatFill.price, HatFill.fees, HatOrder.purpose)
        .join(HatOrder, HatOrder.id == HatFill.order_id)
        .where(HatOrder.signal_id == signal.id, HatOrder.side == "sell")
        .order_by(HatFill.filled_at, HatOrder.placed_at)
    ).all()
    partial_rules = iter(
        str(payload.get("rule") or closing)
        for payload in session.scalars(
            select(HatEvent.payload)
            .where(
                HatEvent.aggregate_type == "position",
                HatEvent.aggregate_id == position.id,
                HatEvent.event_type == "exit.partial",
            )
            .order_by(HatEvent.id)
        )
    )
    return tuple(
        LegRow(
            day=trade_date_of(as_utc(at)),
            qty=qty,
            price=Decimal(price),
            fees=Decimal(fees),
            rule=next(partial_rules, closing) if purpose == "exit_partial" else closing,
        )
        for at, qty, price, fees, purpose in sales
    )


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
        sold = session.execute(
            select(HatFill.qty, HatFill.price)
            .join(HatOrder, HatOrder.id == HatFill.order_id)
            .where(HatOrder.signal_id == signal.id, HatOrder.side == "sell")
        ).all()
        shares = sum(q for q, _ in sold)
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
                dividends=Decimal(result.dividends),
                r_multiple=Decimal(result.r_multiple),
                mfe_r=Decimal(result.mfe_r),
                mae_r=Decimal(result.mae_r),
                holding_days=result.holding_days,
                strategy_version_id=signal.strategy_version_id,
                risk_policy_version_id=signal.risk_policy_version_id,
                score=scores.get((signal.playbook, str(signal.instrument_id), signal.trade_date)),
                # the mean sale price over every leg, the stop when it closed, the rules fired
                exit_price=sum((q * Decimal(p) for q, p in sold), Decimal(0)) / shares
                if shares
                else None,
                final_stop=Decimal(position.current_stop),
                rules_fired=tuple(position.rules_fired or ()),
                legs=_legs(session, signal, position, result.exit_reason),
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


def fills_on(session: Session, day: date) -> list[tuple[str, str, int, Decimal, Decimal]]:
    """Every fill of trade date `day`, in fill order: (side, instrument id, qty, price, fees),
    for the overlay's tax book."""
    start, end = day_bounds(day)
    return [
        (side, str(instrument_id), qty, Decimal(price), Decimal(fees))
        for side, instrument_id, qty, price, fees in session.execute(
            select(HatOrder.side, HatSignal.instrument_id, HatFill.qty, HatFill.price, HatFill.fees)
            .join(HatOrder, HatOrder.id == HatFill.order_id)
            .join(HatSignal, HatSignal.id == HatOrder.signal_id)
            .where(HatFill.filled_at >= start, HatFill.filled_at < end)
            .order_by(HatFill.filled_at, HatOrder.placed_at, HatFill.id)
        )
    ]
