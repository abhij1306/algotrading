"""The read-only HAT dashboard routes under `/api/hat` (HAT-DEC-T3, HAT-OPS-072).

Every route is a GET that reads Postgres and changes nothing: Telegram is the control surface
(kill, re-arm, mode, flatten), the dashboard only shows. `/status` reports what the database
holds now: bots with their mode and manage-only reasons, the kill switches, the latest regime,
today's broker sessions, today's job runs and the latest `feed.stale` of the day. `/books` is
each live and paper book through the last finished session (`daily.books`, on the official
closes in the bar store).

Money, weights and scores are exact decimals and leave as strings, never floats. A failed read
answers a fixed message; database text never reaches the client.

Bots, kill switches, open positions and watchlists come from the shared read model
(`app/hat/read`, which Telegram renders too); the rest the module reads itself. It imports no
module that writes (the control plane, the Telegram reads, the mode code), so no route can reach a
write (CONF `hat_routes_read_only`, HAT-OVR-061).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ...database import get_db
from ...paths import HAT_DATA_ROOT
from ..daily import books as daily_books
from ..data.store import DATASET_BARS, DataStore
from ..engine.confidence import stored_basis
from ..engine.portfolio.regime import RegimeState
from ..events import recorded_for
from ..execution.broker import SessionStatus
from ..governance import ModeField
from ..models import (
    HatAllocationDaily,
    HatEvent,
    HatJobRun,
    HatSignal,
)
from ..models.runtime import SignalConfidence, TradeState
from ..read import bots as read_bots
from ..read import kill as read_kill
from ..read import positions as read_positions
from ..read.regime import latest_regimes
from ..read.sessions import broker_sessions
from ..read.watchlist import latest_watchlists
from ..risk import kill
from ..time import as_utc, day_bounds, trade_date_of, utc_now

__all__ = ["current_time", "router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hat", tags=["HAT dashboard"])

DB_ERROR = "The HAT database could not be read"
READ_ERROR = "The HAT dashboard could not be read"
FEED_STALE = "feed.stale"
UNKNOWN = read_positions.UNKNOWN
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
ERRORS: dict[int | str, dict[str, Any]] = {
    503: {"description": "The database could not be read"},
    500: {"description": "The read failed"},
}

T = TypeVar("T")


def current_time() -> datetime:
    """Now, as an aware UTC instant (a dependency, so a check can fix the clock)."""
    return utc_now()


Db = Annotated[Session, Depends(get_db)]
Now = Annotated[datetime, Depends(current_time)]
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]


def _read(what: str, read: Callable[[], T]) -> T:
    """Run a read; any failure is a fixed message (503 for the database, else 500)."""
    try:
        return read()
    except SQLAlchemyError:
        logger.exception("HAT dashboard %s: database read failed", what)
        raise HTTPException(status_code=503, detail=DB_ERROR) from None
    except Exception:
        logger.exception("HAT dashboard %s: read failed", what)
        raise HTTPException(status_code=500, detail=READ_ERROR) from None


def _money(value: Decimal | int | str) -> str:
    """An exact decimal as its string (never a float)."""
    return str(Decimal(value))


# --- response models ---------------------------------------------------------------------------


class BotView(BaseModel):
    id: uuid.UUID
    name: str
    execution_mode: ModeField  # the mode asked for (`hat_bot.execution_mode` or a change now due)
    effective_mode: ModeField  # the mode an entry is routed by now
    mode_reason: str | None  # why they differ: `sleeve_<state>` or a gate's code (HAT-GOV-085)
    pending_mode: ModeField | None  # a queued change still waiting for its session open
    pending_from: datetime | None
    status: str
    lifecycle_state: str
    risk_cluster: str
    manage_only: bool
    reasons: list[str]


class KillSwitchView(BaseModel):
    scope: str
    bot_id: uuid.UUID | None
    channel: kill.KillChannel
    state: kill.SwitchState


class RegimeView(BaseModel):
    trade_date: date
    state: RegimeState
    confirmed: bool


class BrokerSessionView(BaseModel):
    account: str
    status: SessionStatus


class JobRunView(BaseModel):
    job: str
    trade_date: date
    status: str
    started_at: datetime
    finished_at: datetime | None
    calls: int
    rows: int
    error: str | None


class StatusResponse(BaseModel):
    as_of: datetime
    trade_date: date
    kill_account: bool
    feed_stale_at: datetime | None
    regime: RegimeView | None
    bots: list[BotView]
    kill_switches: list[KillSwitchView]
    sessions: list[BrokerSessionView]
    jobs: list[JobRunView]


class WatchlistItem(BaseModel):
    symbol: str
    score: str


class WatchlistView(BaseModel):
    bot_id: uuid.UUID
    bot: str
    trade_date: date | None
    entries: list[WatchlistItem]


class ConfidenceBasis(BaseModel):
    """What the dashboard shows of a signal's `confidence_basis`: the reason code and the
    backtest trades of its bucket, each null when the stored basis has none."""

    reason: str | None
    trades: int | None


class SignalView(BaseModel):
    id: uuid.UUID
    bot_id: uuid.UUID
    bot: str
    symbol: str
    playbook: str
    trade_date: date
    triggered_at: datetime
    trigger_price: str
    stop_price: str
    qty: int
    risk_amount: str
    mode: ModeField
    state: TradeState
    # the label and its backtest bucket (D132); null on a signal stored without one
    confidence: SignalConfidence | None = None
    confidence_basis: ConfidenceBasis


class PositionView(BaseModel):
    id: uuid.UUID
    bot_id: uuid.UUID
    bot: str
    symbol: str
    qty_open: int
    avg_entry: str
    initial_stop: str
    current_stop: str
    opened_at: datetime
    state: str
    is_virtual: bool


class AllocationRow(BaseModel):
    bot_id: uuid.UUID
    bot: str
    base_weight: str
    regime_mult: str
    health_mult: str
    final_weight: str
    capital: str
    risk_pct: str
    currency: str


class AllocationsResponse(BaseModel):
    trade_date: date | None
    rows: list[AllocationRow]


class EventView(BaseModel):
    id: int
    occurred_at: datetime
    recorded_at: datetime
    event_type: str
    actor: str
    aggregate_type: str
    aggregate_id: uuid.UUID
    bot_id: uuid.UUID | None
    correlation_id: uuid.UUID | None
    payload: dict[str, Any]


# --- reads -------------------------------------------------------------------------------------


def _bot_view(row: read_bots.BotRow) -> BotView:
    return BotView(
        id=row.id,
        name=row.label,
        execution_mode=row.mode.asked,
        effective_mode=row.mode.effective,
        mode_reason=row.mode.why,
        pending_mode=row.mode.pending,
        pending_from=row.mode.pending_from,
        status=row.status,
        lifecycle_state=row.lifecycle_state,
        risk_cluster=row.risk_cluster,
        manage_only=row.kill.manage_only,
        reasons=list(row.reasons),
    )


def _bots(session: Session, now: datetime, rows: list[kill.KillRow]) -> list[BotView]:
    return [_bot_view(row) for row in read_bots.bot_rows(session, now, rows)]


def _sessions(session: Session, day: date) -> list[BrokerSessionView]:
    """Today's login state of every active broker account (`missing` when none is recorded)."""
    return [
        BrokerSessionView(account=row.label, status=row.status)
        for row in broker_sessions(session, day)
    ]


def _kill_switches(rows: list[kill.KillRow]) -> list[KillSwitchView]:
    """The current state of every (scope, bot, channel) key that has a row."""
    keys = sorted(
        {(r.scope, r.bot_id, kill.channel_of(r.reason)) for r in rows},
        key=lambda k: (k[0], str(k[1] or ""), k[2]),
    )
    return [
        KillSwitchView(
            scope=scope,
            bot_id=bot_id,
            channel=channel,
            state=kill.current_state(rows, scope, bot_id, channel),
        )
        for scope, bot_id, channel in keys
    ]


def _regime(session: Session) -> RegimeView | None:
    """The latest regime row, if any."""
    found = latest_regimes(session, 1)
    if not found:
        return None
    return RegimeView(
        trade_date=found[0].trade_date, state=found[0].state, confirmed=found[0].confirmed
    )


def _jobs(session: Session, day: date) -> list[JobRunView]:
    rows = session.scalars(
        select(HatJobRun).where(HatJobRun.trade_date == day).order_by(HatJobRun.job)
    )
    return [
        JobRunView(
            job=r.job,
            trade_date=r.trade_date,
            status=r.status,
            started_at=as_utc(r.started_at),
            finished_at=as_utc(r.finished_at) if r.finished_at is not None else None,
            calls=r.calls,
            rows=r.rows,
            error=r.error,
        )
        for r in rows
    ]


def _feed_stale_at(session: Session, day: date) -> datetime | None:
    start, end = day_bounds(day)
    found = session.scalar(
        select(func.max(HatEvent.occurred_at)).where(
            HatEvent.event_type == FEED_STALE,
            recorded_for(day),
            HatEvent.occurred_at >= start,
            HatEvent.occurred_at < end,
        )
    )
    return as_utc(found) if found is not None else None


def _status(session: Session, now: datetime) -> StatusResponse:
    day = trade_date_of(now)
    rows = read_kill.kill_rows(session)
    return StatusResponse(
        as_of=now,
        trade_date=day,
        kill_account=read_kill.account_tripped(rows),
        feed_stale_at=_feed_stale_at(session, day),
        regime=_regime(session),
        bots=_bots(session, now, rows),
        kill_switches=_kill_switches(rows),
        sessions=_sessions(session, day),
        jobs=_jobs(session, day),
    )


def _watchlists(session: Session, now: datetime) -> list[WatchlistView]:
    """Each bot's latest watchlist on or before today: one row per symbol at its best score,
    best first (score, then symbol)."""
    found = latest_watchlists(session, trade_date_of(now))
    bots = read_bots.all_bots(session)
    labels = read_bots.labels(session, (bot.id for bot in bots))
    out = []
    for bot in bots:
        watchlist = found.get(bot.id)
        out.append(
            WatchlistView(
                bot_id=bot.id,
                bot=labels[bot.id],
                trade_date=watchlist.trade_date if watchlist else None,
                entries=[
                    WatchlistItem(symbol=symbol, score=_money(score))
                    for symbol, score in (watchlist.entries if watchlist else ())
                ],
            )
        )
    return out


def _basis(raw: object) -> ConfidenceBasis:
    basis = stored_basis(raw)
    reason, trades = basis.get("reason"), basis.get("trades")
    return ConfidenceBasis(
        reason=reason if isinstance(reason, str) else None,
        trades=trades if isinstance(trades, int) and not isinstance(trades, bool) else None,
    )


def _signals(session: Session, limit: int) -> list[SignalView]:
    rows = list(
        session.scalars(
            select(HatSignal).order_by(HatSignal.triggered_at.desc(), HatSignal.id).limit(limit)
        )
    )
    labels = read_bots.labels(session, (s.bot_id for s in rows))
    symbols = read_positions.symbols(session, (s.instrument_id for s in rows))
    return [
        SignalView(
            id=s.id,
            bot_id=s.bot_id,
            bot=labels.get(s.bot_id, UNKNOWN),
            symbol=symbols.get(s.instrument_id, UNKNOWN),
            playbook=s.playbook,
            trade_date=s.trade_date,
            triggered_at=as_utc(s.triggered_at),
            trigger_price=_money(s.trigger_price),
            stop_price=_money(s.stop_price),
            qty=s.qty,
            risk_amount=_money(s.risk_amount),
            mode=s.mode,
            state=s.state,
            confidence=s.confidence,
            confidence_basis=_basis(s.confidence_basis),
        )
        for s in rows
    ]


def _positions(session: Session) -> list[PositionView]:
    return [
        PositionView(
            id=p.id,
            bot_id=p.bot_id,
            bot=p.bot,
            symbol=p.symbol,
            qty_open=p.qty_open,
            avg_entry=_money(p.avg_entry),
            initial_stop=_money(p.initial_stop),
            current_stop=_money(p.current_stop),
            opened_at=p.opened_at,
            state=p.state,
            is_virtual=p.is_virtual,
        )
        for p in read_positions.open_positions(session)
    ]


def _allocations(session: Session) -> AllocationsResponse:
    day = session.scalar(select(func.max(HatAllocationDaily.trade_date)))
    if day is None:
        return AllocationsResponse(trade_date=None, rows=[])
    rows = list(
        session.scalars(
            select(HatAllocationDaily)
            .where(HatAllocationDaily.trade_date == day)
            .order_by(HatAllocationDaily.bot_id)
        )
    )
    labels = read_bots.labels(session, (a.bot_id for a in rows))
    return AllocationsResponse(
        trade_date=day,
        rows=[
            AllocationRow(
                bot_id=a.bot_id,
                bot=labels.get(a.bot_id, UNKNOWN),
                base_weight=_money(a.base_weight),
                regime_mult=_money(a.regime_mult),
                health_mult=_money(a.health_mult),
                final_weight=_money(a.final_weight),
                capital=_money(a.capital),
                risk_pct=_money(a.risk_pct),
                currency=a.currency,
            )
            for a in rows
        ],
    )


def _events(session: Session, limit: int) -> list[EventView]:
    rows = session.scalars(select(HatEvent).order_by(HatEvent.id.desc()).limit(limit))
    return [
        EventView(
            id=e.id,
            occurred_at=as_utc(e.occurred_at),
            recorded_at=as_utc(e.recorded_at),
            event_type=e.event_type,
            actor=e.actor,
            aggregate_type=e.aggregate_type,
            aggregate_id=e.aggregate_id,
            bot_id=e.bot_id,
            correlation_id=e.correlation_id,
            payload=dict(e.payload or {}),
        )
        for e in rows
    ]


class BookPositionView(BaseModel):
    symbol: str
    qty: int
    avg_entry: str
    initial_stop: str
    current_stop: str
    mark: str | None
    value: str
    unrealised: str
    open_r: str | None
    opened: date


class BookTradeView(BaseModel):
    symbol: str
    entry: date
    exit: date
    hold: int
    exit_reason: str
    net_pnl: str
    fees: str
    r: str


class BookView(BaseModel):
    """One live or paper book: money as exact strings; `metrics` the Backtest Result
    Standard's headline metrics, as a run's `run.json` holds them."""

    bot: str
    kind: str
    as_of: date
    period_start: date
    start_capital: str
    equity: str
    day_pnl: str
    realised: str
    realised_today: str
    unrealised: str
    fees: str
    fees_today: str
    position_value: str
    invested_pct: float | None
    benchmark_return_pct: float | None
    metrics: dict[str, float | int | bool | str | None]
    positions: list[BookPositionView]
    trades: list[BookTradeView]


class BooksResponse(BaseModel):
    as_of: date
    books: list[BookView]


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _book(r: daily_books.BookReport) -> BookView:
    return BookView(
        bot=r.bot,
        kind=r.kind,
        as_of=r.as_of,
        period_start=r.period_start,
        start_capital=str(r.start_capital),
        equity=str(r.equity),
        day_pnl=str(r.day_pnl),
        realised=str(r.realised),
        realised_today=str(r.realised_today),
        unrealised=str(r.unrealised),
        fees=str(r.fees),
        fees_today=str(r.fees_today),
        position_value=str(r.position_value),
        invested_pct=r.invested_pct,
        benchmark_return_pct=r.benchmark_return_pct,
        metrics=dict(r.metrics),
        positions=[
            BookPositionView(
                symbol=p.symbol,
                qty=p.qty,
                avg_entry=str(p.avg_entry),
                initial_stop=str(p.initial_stop),
                current_stop=str(p.current_stop),
                mark=_text(p.mark),
                value=str(p.value),
                unrealised=str(p.unrealised),
                open_r=_text(p.open_r),
                opened=p.opened,
            )
            for p in r.positions
        ],
        trades=[
            BookTradeView(
                symbol=t.symbol,
                entry=t.entry,
                exit=t.exit,
                hold=t.hold,
                exit_reason=t.exit_reason,
                net_pnl=str(t.net_pnl),
                fees=str(t.fees),
                r=str(t.r),
            )
            for t in r.trades
        ],
    )


def _books(db: Session, now: datetime) -> BooksResponse:
    """The books through the last stored session on or before today (IST)."""
    store = DataStore(HAT_DATA_ROOT)
    today = trade_date_of(now)
    stored = [d for d in store.days(DATASET_BARS) if d <= today]
    as_of = stored[-1] if stored else today
    return BooksResponse(
        as_of=as_of, books=[_book(r) for r in daily_books.read_books(db, store, as_of)]
    )


# --- routes ------------------------------------------------------------------------------------


@router.get("/status", responses=ERRORS)
def hat_status(db: Db, now: Now) -> StatusResponse:
    """What the database holds now: bots, kill switches, regime, sessions and today's jobs."""
    return _read("status", lambda: _status(db, now))


@router.get("/watchlist", responses=ERRORS)
def hat_watchlist(db: Db, now: Now) -> list[WatchlistView]:
    """Each bot's latest watchlist on or before today, best score first."""
    return _read("watchlist", lambda: _watchlists(db, now))


@router.get("/signals", responses=ERRORS)
def hat_signals(db: Db, limit: Limit = DEFAULT_LIMIT) -> list[SignalView]:
    """The latest signals, newest first."""
    return _read("signals", lambda: _signals(db, limit))


@router.get("/positions", responses=ERRORS)
def hat_positions(db: Db) -> list[PositionView]:
    """The open positions, oldest first."""
    return _read("positions", lambda: _positions(db))


@router.get("/allocations", responses=ERRORS)
def hat_allocations(db: Db) -> AllocationsResponse:
    """The latest nightly allocation, one row per bot."""
    return _read("allocations", lambda: _allocations(db))


@router.get("/events", responses=ERRORS)
def hat_events(db: Db, limit: Limit = DEFAULT_LIMIT) -> list[EventView]:
    """The latest events of the log, newest first."""
    return _read("events", lambda: _events(db, limit))


@router.get("/books", responses=ERRORS)
def hat_books(db: Db, now: Now) -> BooksResponse:
    """Each live and paper book through the last stored session: P&L, costs, invested share,
    trades' R and the headline metrics against the NIFTY 500 TRI."""
    return _read("books", lambda: _books(db, now))
