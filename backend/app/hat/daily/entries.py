"""The live entries of the trading day (PR 17): the inputs of the risk check and where a
triggered entry goes.

- `LiveContexts`: the `ContextSource` of the trigger service (equity, allocation, cash, gate,
  quality, kill, ladder and policy versions, calendar).
- `LiveRoute`: the `Route` of the trigger service. A bot whose broker account has a desk goes
  through that account's `ExecutionRouter` with a `PlacementCheck` read at placement time;
  any other bot through `route_by_mode` (shadow only).

A missing input never places anything: the risk check rejects an entry with a missing input.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import Clock
from ..data.instruments import current_industry
from ..engine import trades
from ..engine.live_entry import route_by_mode
from ..execution.modes import bot_mode
from ..execution.router import ExecutionRouter, PlacementCheck
from ..models import HatAllocationDaily, HatBot, HatPosition, HatTradeResult
from ..risk import controls
from ..risk.decision import SignalContext
from ..risk.modes import Mode
from ..risk.policy_service import load_versions
from ..risk.service import evaluate_signal
from ..risk.sessions import SessionCalendar
from ..time import trade_date_of, utc_now
from .gates import CalendarSource, GateBoard
from .prices import LivePrices

__all__ = ["LiveContexts", "LiveRoute"]

RouterFor = Callable[[uuid.UUID], ExecutionRouter | None]
FundsFor = Callable[[uuid.UUID], Decimal | None]


def _allocation(
    session: Session, bot_id: uuid.UUID, day: date, calendar: SessionCalendar | None
) -> HatAllocationDaily | None:
    """The sleeve's allocation for `day`: the one `allocate` made the night before (the previous
    trading day). None when that night's allocation is missing (for example the broker funds
    could not be read), so the entry fails closed rather than trade on an older weight or an
    unmeasured drawdown."""
    made = calendar.previous_trading_day(day) if calendar is not None else None
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
    realised P&L in the bot's mode; free cash is the broker's available funds for a real
    entry and the sleeve's equity less its open virtual positions for a shadow one. A virtual
    entry of a Shadow or Probation sleeve (weight 0) is sized from its nominal allocation
    (`virtual_capital`, `virtual_risk_pct`, health 1; HAT-PORT-050); a real entry never is."""

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
        virtual = bot_mode(session, bot, at, calendar) == Mode.SHADOW
        # a Shadow or Probation sleeve has weight 0; its virtual entries use the nominal
        # allocation, health 1, and the book they are capped against counts it (HAT-PORT-050)
        nominal = virtual and allocation.virtual_capital is not None
        capital = Decimal(allocation.virtual_capital if nominal else allocation.capital)
        risk_pct = Decimal(allocation.virtual_risk_pct if nominal else allocation.risk_pct)
        equity = capital + _realised(session, bot_id, virtual)
        book = (
            func.coalesce(HatAllocationDaily.virtual_capital, HatAllocationDaily.capital)
            if virtual
            else HatAllocationDaily.capital
        )
        total = session.scalar(
            select(func.sum(book)).where(HatAllocationDaily.trade_date == allocation.trade_date)
        )
        cash = (
            equity - _open_notional(session, bot_id, True)
            if virtual
            else self._funds(bot.broker_account_id)
        )
        return copy.replace(
            base,
            equity=equity,
            allocation_risk_pct=risk_pct,
            regime_mult=Decimal(allocation.regime_mult),
            health_mult=Decimal(1) if nominal else Decimal(allocation.health_mult),
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
        prices: LivePrices,
    ) -> None:
        self._routers = routers
        self._gates = gates
        self._feed_stale = feed_stale
        self._clock = clock
        self._calendar = calendar
        self._prices = prices

    def check(
        self, session: Session, bot_id: uuid.UUID, instrument_id: uuid.UUID, now: datetime
    ) -> PlacementCheck:
        calendar = self._calendar()
        ltp, refusal = self._prices.checked(session, instrument_id, now)
        return PlacementCheck(
            calendar=calendar,
            feed_stale=self._feed_stale(),
            quality_failed=self._gates.quality_failed(session, trade_date_of(now), calendar),
            gate_on=self._gates.gate_on(session, bot_id, now),
            ltp=ltp,
            ltp_refusal=refusal,
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
            session,
            request,
            now=now,
            check=self.check(session, request.bot_id, request.instrument_id, now),
            decide=decide,
        )
