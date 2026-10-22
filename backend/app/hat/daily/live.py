"""The live trading day (10-ops §3 steps 1-6, PR 17): what the trigger, the router, the exits and
the Telegram commands read while the market is open.

- `PriceBook`: the feed's last trade per instrument (a tick listener on the loop thread).
- `GateBoard`: each bot's regime gate, evaluated at or after 09:20 IST of the trade date and kept
  for the day; before 09:20 it is unknown, so no entry is accepted (HAT-RISK-051). It also tells
  whether last night's quality gate passed for the data the day trades on.
- `LiveContexts`: the `ContextSource` of the trigger service (equity, allocation, cash, gate,
  quality, kill, ladder and policy versions, calendar).
- `LiveRoute`: the `Route` of the trigger service. A bot whose broker account has a desk goes
  through that account's `ExecutionRouter` with a `PlacementCheck` read at placement time;
  any other bot through `route_by_mode` (shadow only).
- `ExitViews`: the `ViewSource` of the exits (live price, days held, SMA and swing lows).
- `ShadowExits`: the live exit rules of every virtual position (a `TradeManager` runs the live
  rules of real positions only), and the 15:20-15:25 rules of the virtual positions of bots whose
  account has no `TradeManager` (its `exits.close` covers its virtual positions too).

Missing data never fires anything: an unknown price, gate or quality state leaves the position or
the entry alone (the risk check rejects an entry with a missing input).
"""

from __future__ import annotations

import copy
import logging
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import Clock
from ..data.instruments import current_industry
from ..data.quality import JOB_QUALITY
from ..data.store import DataStore
from ..engine import trades
from ..engine.exits import ExitView, Phase
from ..engine.live_entry import route_by_mode
from ..engine.live_trades import record_close_evaluation
from ..engine.regime_gate import regime_allows_longs
from ..engine.strategy.config import parse_config
from ..execution.manage import in_close_window
from ..execution.modes import SHADOW, bot_mode
from ..execution.router import ExecutionRouter, PlacementCheck
from ..models import (
    HatAllocationDaily,
    HatBot,
    HatInstrument,
    HatJobRun,
    HatPosition,
    HatSignal,
    HatStrategyVersion,
    HatTradeResult,
)
from ..ops.jobs import RunStatus
from ..risk import controls
from ..risk.decision import SignalContext
from ..risk.policy_service import load_versions
from ..risk.service import evaluate_signal
from ..risk.sessions import SessionCalendar
from ..time import IST, trade_date_of, utc_now
from .market import DailyBook, held_sessions, index_closes, sessions_before

__all__ = [
    "GATE_AT",
    "ExitViews",
    "GateBoard",
    "LiveContexts",
    "LiveRoute",
    "PriceBook",
    "ShadowExits",
]

logger = logging.getLogger(__name__)

GATE_AT = time(9, 20)  # the first 5-minute bar is final; the gate is evaluated again from here

RouterFor = Callable[[uuid.UUID], ExecutionRouter | None]
FundsFor = Callable[[uuid.UUID], Decimal | None]


class PriceBook:
    """The last traded price per instrument from the feed. `on_tick` runs on the loop thread;
    `price` may be read from any thread (a dict read of an immutable value)."""

    def __init__(self) -> None:
        self._prices: dict[str, Decimal] = {}
        self._symbols: dict[uuid.UUID, str] = {}

    def on_tick(self, payload: Mapping[str, Any]) -> None:
        symbol = payload.get("symbol")
        raw = payload.get("ltp")
        if not isinstance(symbol, str) or raw is None or isinstance(raw, bool):
            return
        try:
            price = Decimal(str(raw))
        except InvalidOperation:
            return
        if price.is_finite() and price > 0:
            self._prices[symbol] = price

    def track(self, symbols: Mapping[uuid.UUID, str]) -> None:
        """The instruments whose prices are read by id (the day's subscriptions)."""
        self._symbols = {**self._symbols, **symbols}

    def track_session(self, session: Session, instrument_ids: set[uuid.UUID]) -> None:
        if instrument_ids:
            self.track(
                dict(
                    session.execute(
                        select(HatInstrument.id, HatInstrument.symbol).where(
                            HatInstrument.id.in_(instrument_ids)
                        )
                    ).all()
                )
            )

    def reset(self) -> None:
        """A new trading day: yesterday's prices are not today's."""
        self._prices = {}

    def price(self, instrument_id: uuid.UUID) -> Decimal | None:
        symbol = self._symbols.get(instrument_id)
        return None if symbol is None else self._prices.get(symbol)


# The trading calendar the runtime keeps (read again every 10 minutes), None while unknown.
CalendarSource = Callable[[], SessionCalendar | None]


def _quality_failed(session: Session, day: date, calendar: SessionCalendar | None) -> bool | None:
    """Whether the quality gate of the data `day` trades on failed: the gate run of the previous
    trading day (the night before). None when that run did not finish or does not exist, so
    entries fail closed."""
    wanted = _previous_trading_day(calendar, day)
    if wanted is None:
        return None
    status = session.scalar(
        select(HatJobRun.status).where(HatJobRun.job == JOB_QUALITY, HatJobRun.trade_date == wanted)
    )
    if status == RunStatus.SUCCEEDED.value:
        return False
    if status == RunStatus.FAILED.value:
        return True
    return None


def _previous_trading_day(calendar: SessionCalendar | None, day: date) -> date | None:
    if calendar is None:
        return None
    earlier = [d for d, row in calendar.days.items() if d < day and row.is_trading]
    return max(earlier) if earlier else None


class GateBoard:
    """Each bot's regime gate for the trade date, evaluated once at or after 09:20 IST on the
    index closes before the trade date (HAT-RISK-051), and the quality-gate state of the day."""

    def __init__(self, store: DataStore) -> None:
        self._store = store
        self._gates: dict[tuple[uuid.UUID, date], bool] = {}
        self._lock = threading.Lock()

    def gate_on(self, session: Session, bot_id: uuid.UUID, at: datetime) -> bool | None:
        day = trade_date_of(at)
        if at.astimezone(IST).time() < GATE_AT:
            return None
        key = (bot_id, day)
        with self._lock:
            if key in self._gates:
                return self._gates[key]
        bot = session.get(HatBot, bot_id)
        version = session.get(HatStrategyVersion, bot.live_version_id) if bot else None
        if version is None:
            return None
        regime = parse_config(version.config).regime
        before = sessions_before(self._store, day, 1)
        if not before:
            return None
        gate = regime_allows_longs(regime, index_closes(self._store, regime.index, before[-1]))
        with self._lock:
            self._gates = {k: v for k, v in self._gates.items() if k[1] == day}
            self._gates[key] = gate
        logger.info("regime gate evaluated", extra={"bot_id": str(bot_id), "gate_on": gate})
        return gate

    @staticmethod
    def quality_failed(
        session: Session, day: date, calendar: SessionCalendar | None
    ) -> bool | None:
        return _quality_failed(session, day, calendar)


def _allocation(
    session: Session, bot_id: uuid.UUID, day: date, calendar: SessionCalendar | None
) -> HatAllocationDaily | None:
    """The sleeve's allocation for `day`: the one `allocate` made the night before (the previous
    trading day). None when that night's allocation is missing (for example the broker funds
    could not be read), so the entry fails closed rather than trade on an older weight or an
    unmeasured drawdown."""
    made = _previous_trading_day(calendar, day)
    if made is None:
        return None
    return session.get(HatAllocationDaily, (made, bot_id))


def _realised(session: Session, bot_id: uuid.UUID, virtual: bool) -> Decimal:
    rows = session.scalars(
        select(HatTradeResult.realized_pnl)
        .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
        .where(HatPosition.bot_id == bot_id, HatPosition.is_virtual.is_(virtual))
    )
    return sum((Decimal(r) for r in rows), Decimal(0))


def _open_notional(session: Session, bot_id: uuid.UUID, virtual: bool) -> Decimal:
    rows = session.execute(
        select(HatPosition.avg_entry, HatPosition.qty_open).where(
            HatPosition.bot_id == bot_id,
            HatPosition.is_virtual.is_(virtual),
            HatPosition.closed_at.is_(None),
        )
    ).all()
    return sum((Decimal(entry) * qty for entry, qty in rows), Decimal(0))


class LiveContexts:
    """The `ContextSource` of live entries. Equity is the sleeve's allocation capital plus its
    realised P&L (06 §2) in the bot's mode; free cash is the broker's available funds for a real
    entry and the sleeve's equity less its open virtual positions for a shadow one."""

    def __init__(
        self,
        gates: GateBoard,
        user_id: uuid.UUID,
        feed_stale: Callable[[], bool | None],
        funds: FundsFor,
        calendar: CalendarSource,
    ) -> None:
        self._gates = gates
        self._user_id = user_id
        self._feed_stale = feed_stale
        self._funds = funds
        self._calendar = calendar

    def entry_context(self, session: Session, bot_id: uuid.UUID, at: datetime) -> SignalContext:
        day = trade_date_of(at)
        bot = session.get(HatBot, bot_id)
        calendar = self._calendar()
        base = SignalContext(
            calendar=calendar,
            gate_on=self._gates.gate_on(session, bot_id, at),
            feed_stale=self._feed_stale(),
            quality_failed=self._gates.quality_failed(session, day, calendar),
            kill=controls.kill_state(session, bot_id),
            ladder=controls.ladder_state(session, bot_id),
            policy_versions=load_versions(session, self._user_id),
        )
        allocation = _allocation(session, bot_id, day, calendar)
        if bot is None or allocation is None:
            return base  # no allocation: the risk check rejects for the missing input
        virtual = bot_mode(session, bot, at, calendar) == SHADOW
        capital = Decimal(allocation.capital)
        equity = capital + _realised(session, bot_id, virtual)
        total = session.scalar(
            select(func.sum(HatAllocationDaily.capital)).where(
                HatAllocationDaily.trade_date == allocation.trade_date
            )
        )
        cash = (
            equity - _open_notional(session, bot_id, True)
            if virtual
            else self._funds(bot.broker_account_id)
        )
        return copy.replace(
            base,
            equity=equity,
            allocation_risk_pct=Decimal(allocation.risk_pct),
            regime_mult=Decimal(allocation.regime_mult),
            health_mult=Decimal(allocation.health_mult),
            total_equity=None if total is None else Decimal(total),
            free_cash=cash,
        )


def _with_industry(session: Session, request: trades.EntryRequest) -> trades.EntryRequest:
    found = current_industry(session, request.instrument_id, request.trade_date)
    industry = found.industry if found is not None else "unknown"
    return copy.replace(request, context=copy.replace(request.context, industry=industry))


class LiveRoute:
    """The trigger service's `Route`: through the bot's account router when it has a desk, else
    `route_by_mode`. The placement check is read here, at placement (not the trigger's
    snapshot)."""

    def __init__(
        self,
        routers: RouterFor,
        gates: GateBoard,
        feed_stale: Callable[[], bool | None],
        clock: Clock,
        calendar: CalendarSource,
    ) -> None:
        self._routers = routers
        self._gates = gates
        self._feed_stale = feed_stale
        self._clock = clock
        self._calendar = calendar

    def check(self, session: Session, bot_id: uuid.UUID, now: datetime) -> PlacementCheck:
        calendar = self._calendar()
        return PlacementCheck(
            calendar=calendar,
            feed_stale=self._feed_stale(),
            quality_failed=self._gates.quality_failed(session, trade_date_of(now), calendar),
            gate_on=self._gates.gate_on(session, bot_id, now),
        )

    def __call__(
        self,
        session: Session,
        request: trades.EntryRequest,
        *,
        decide: trades.DECIDE = evaluate_signal,
    ) -> trades.EntryOutcome:
        request = _with_industry(session, request)
        bot = session.get(HatBot, request.bot_id)
        router = self._routers(bot.broker_account_id) if bot is not None else None
        if router is None:
            return route_by_mode(session, request, decide=decide)
        now = utc_now(self._clock)
        return router.route(
            session, request, now=now, check=self.check(session, request.bot_id, now), decide=decide
        )


class ExitViews:
    """The exits' `ViewSource`: the live price, the sessions held and the daily levels of the
    position's pinned exit rules. None without a live price."""

    def __init__(
        self,
        book: DailyBook,
        prices: Callable[[uuid.UUID], Decimal | None],
        clock: Clock,
        calendar: CalendarSource,
    ) -> None:
        self._book = book
        self._prices = prices
        self._clock = clock
        self._calendar = calendar

    def __call__(self, session: Session, position: HatPosition) -> ExitView | None:
        price = self._prices(position.instrument_id)
        if price is None:
            return None
        today = trade_date_of(utc_now(self._clock))
        signal = session.get(HatSignal, position.signal_id)
        if signal is None:
            return None
        calendar = self._calendar()
        trading = [d for d, row in calendar.days.items() if row.is_trading] if calendar else []
        held = held_sessions(trading, trade_date_of(position.opened_at), today)
        rules = trades.exit_rules_of(session, signal)
        return self._book.day(today).exit_view(position.instrument_id, price, rules, held)


class ShadowExits:
    """The exit rules of the virtual positions: live rules on each pass for all of them (no
    `TradeManager` runs them), and the close-phase rules once in the 15:20-15:25 window for those
    no `exits.close` handles (with the evaluation record the nightly close gap reads)."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        views: ExitViews,
        managed: Callable[[], set[uuid.UUID]],
    ) -> None:
        self._session_factory = session_factory
        self._views = views
        self._managed = managed  # accounts whose exits.close covers their virtual positions

    def _positions(self, session: Session, phase: Phase) -> list[HatPosition]:
        """Every open virtual position for the live rules (a `TradeManager` runs the live rules
        of real positions only); for the close phase, only those of accounts without a manager
        (an account's `exits.close` evaluates its virtual positions itself)."""
        query = (
            select(HatPosition)
            .join(HatBot, HatBot.id == HatPosition.bot_id)
            .where(HatPosition.is_virtual.is_(True), HatPosition.closed_at.is_(None))
            .order_by(HatPosition.id)
        )
        accounts = self._managed() if phase == "close" else set()
        if accounts:
            query = query.where(HatBot.broker_account_id.not_in(accounts))
        return list(session.scalars(query))

    def run(self, now: datetime, phase: Phase = "live") -> int:
        """Evaluate the open virtual positions of `phase` once; returns how many were."""
        done = 0
        with self._session_factory() as session:
            for position in self._positions(session, phase):
                view = self._views(session, position)
                if view is None or view.price is None:
                    continue
                try:
                    result = trades.evaluate_position(session, position.id, view, phase, now)
                    if phase == "close":
                        record_close_evaluation(
                            session, position.id, view, result, trade_date_of(now), now
                        )
                    session.commit()
                    done += 1
                except Exception:
                    session.rollback()
                    logger.exception("virtual exits of position %s failed", position.id)
        return done

    def close(self, now: datetime) -> int:
        """The close-phase pass; nothing outside 15:20-15:25 IST."""
        return self.run(now, "close") if in_close_window(now) else 0
