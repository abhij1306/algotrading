"""The live trading day (10-ops §3 steps 1-6, PR 17): what the trigger, the router, the exits and
the Telegram commands read while the market is open.

- `PriceBook`: the feed's last trade per instrument with its exchange time (a `LiveData` tick
  listener, loop thread).
- `LivePrices`: the price a Take, a placement or an exit acts on: the last trade, refused when
  the stale threshold old or outside the day's price band (`data.live.usable_price`, PR 51).
- `stale_feed`: whether the feed is stale now; before a first tick in an open session it is.
- `GateBoard`: each bot's regime gate, evaluated at or after 09:20 IST of the trade date and kept
  for the day; before 09:20 it is unknown, so no entry is accepted (HAT-RISK-051). It also tells
  whether last night's quality gate passed for the data the day trades on.
- `LiveContexts`: the `ContextSource` of the trigger service (equity, allocation, cash, gate,
  quality, kill, ladder and policy versions, calendar).
- `LiveRoute`: the `Route` of the trigger service. A bot whose broker account has a desk goes
  through that account's `ExecutionRouter` with a `PlacementCheck` read at placement time;
  any other bot through `route_by_mode` (shadow only).
- `ExitViews`: the `ViewSource` of the exits (checked live price, days held, SMA and swing
  lows).
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
from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import Clock
from ..data.bars import Tick
from ..data.instruments import current_industry
from ..data.live import Quote, usable_price
from ..data.quality import JOB_QUALITY
from ..data.staleness import StalenessMonitor
from ..data.store import DataStore
from ..engine import trades
from ..engine.exits import ExitView, Phase
from ..engine.live_entry import route_by_mode
from ..engine.live_trades import record_close_evaluation
from ..engine.regime_gate import regime_allows_longs
from ..engine.rescale import manage_only
from ..engine.strategy.config import parse_config
from ..engine.trigger import (
    DailyInputs,
    locked_lower_circuit,
    lower_circuit_price,
    upper_circuit_price,
)
from ..execution.manage import in_close_window
from ..execution.modes import SHADOW, bot_mode
from ..execution.router import ExecutionRouter, PlacementCheck
from ..execution.series import series_changed
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
from ..time import IST, as_utc, trade_date_of, utc_now
from .market import DailyBook, held_sessions, index_closes, sessions_before

__all__ = [
    "GATE_AT",
    "ExitViews",
    "GateBoard",
    "LiveContexts",
    "LivePrices",
    "LiveRoute",
    "PriceBook",
    "SaleBands",
    "ShadowExits",
    "band_bounds",
]

logger = logging.getLogger(__name__)

GATE_AT = time(9, 20)  # the first 5-minute bar is final; the gate is evaluated again from here

RouterFor = Callable[[uuid.UUID], ExecutionRouter | None]
FundsFor = Callable[[uuid.UUID], Decimal | None]


class PriceBook:
    """The last traded price per instrument from the feed, with the exchange time of its tick.
    `on_tick` runs on the loop thread with ticks `LiveData` already parsed (a symbol and a
    finite positive price); `quote` and `price` may be read from any thread (a dict read of an
    immutable value)."""

    def __init__(self) -> None:
        self._prices: dict[str, Quote] = {}
        self._symbols: dict[uuid.UUID, str] = {}

    def on_tick(self, tick: Tick) -> None:
        # A print older than the one kept (by exchange time) arrived late: keep the newer one.
        kept = self._prices.get(tick.symbol)
        if kept is None or tick.at >= kept.at:
            self._prices[tick.symbol] = Quote(tick.price, tick.at)

    def track_session(self, session: Session, instrument_ids: set[uuid.UUID]) -> None:
        """The instruments whose prices are read by id (the day's subscriptions)."""
        if instrument_ids:
            found = session.execute(
                select(HatInstrument.id, HatInstrument.symbol).where(
                    HatInstrument.id.in_(instrument_ids)
                )
            )
            self._symbols = {**self._symbols, **dict(found.all())}

    def reset(self) -> None:
        """A new trading day: yesterday's prices are not today's."""
        self._prices = {}

    def quote(self, instrument_id: uuid.UUID) -> Quote | None:
        symbol = self._symbols.get(instrument_id)
        return None if symbol is None else self._prices.get(symbol)

    def price(self, instrument_id: uuid.UUID) -> Decimal | None:
        """The last traded price, unchecked: for display only (`/positions`)."""
        quote = self.quote(instrument_id)
        return None if quote is None else quote.price


def band_bounds(daily: DailyInputs) -> tuple[Decimal | None, Decimal | None]:
    """The lowest and highest price the day's band lets trade (02 §2), a tick wider each way
    for the exchange's own rounding of the band; None for a bound not known (no band row, an
    F&O name, no previous close)."""
    slack = daily.tick if daily.tick is not None and daily.tick > 0 else Decimal(0)
    lower, upper = lower_circuit_price(daily), upper_circuit_price(daily)
    return (
        None if lower is None else lower - slack,
        None if upper is None else upper + slack,
    )


class SaleBands:
    """The day's lower band of an instrument for its sales (PR 48, `fills.SaleBand`): the lower
    circuit price from the stored band (02 §2), and whether the last trade the feed gave is at it
    (the instrument is locked at its lower circuit). A locked instrument trades seldom, so its
    last trade is read whatever its age."""

    def __init__(self, quotes: PriceBook, book: DailyBook) -> None:
        self._quotes = quotes
        self._book = book

    def _daily(self, session: Session, instrument_id: uuid.UUID, now: datetime) -> DailyInputs:
        return self._book.day(trade_date_of(now)).daily(session, instrument_id)

    def lower(self, session: Session, instrument_id: uuid.UUID, now: datetime) -> Decimal | None:
        return lower_circuit_price(self._daily(session, instrument_id, now))

    def locked(self, session: Session, instrument_id: uuid.UUID, now: datetime) -> bool | None:
        quote = self._quotes.quote(instrument_id)
        if quote is None:
            return None
        return locked_lower_circuit(quote.price, self._daily(session, instrument_id, now))


class LivePrices:
    """The live price a Take, a placement or an exit acts on (PR 51): the instrument's last
    trade from the feed, refused when it is the stale threshold old (HAT-DATA-116) or outside
    the day's price band. `checked` gives the price or None and the refusal's fixed code."""

    def __init__(self, quotes: PriceBook, book: DailyBook, clock: Clock) -> None:
        self._quotes = quotes
        self._book = book
        self._clock = clock

    def checked(
        self, session: Session, instrument_id: uuid.UUID, now: datetime | None = None
    ) -> tuple[Decimal | None, str | None]:
        at = utc_now(self._clock) if now is None else now
        quote = self._quotes.quote(instrument_id)
        lower = upper = None
        if quote is not None:
            daily = self._book.day(trade_date_of(at)).daily(session, instrument_id)
            lower, upper = band_bounds(daily)
        price, refusal = usable_price(quote, at, lower=lower, upper=upper)
        if refusal is not None:
            logger.debug("live price of %s refused: %s", instrument_id, refusal)
        return price, refusal

    def price(self, session: Session, instrument_id: uuid.UUID) -> Decimal | None:
        return self.checked(session, instrument_id)[0]


# The trading calendar the runtime keeps (read again every 10 minutes), None while unknown.
CalendarSource = Callable[[], SessionCalendar | None]


def stale_feed(staleness: StalenessMonitor, now: datetime) -> bool:
    """HAT-DATA-116 read at `now`, not the monitor's last transition: in an open session the feed
    is stale until a tick arrives, so a restart never reads a feed it has not heard from as fresh
    (an entry resumed or triggered then is refused `stale_feed`)."""
    return staleness.is_stale(now)


def _quality_failed(session: Session, day: date, calendar: SessionCalendar | None) -> bool | None:
    """Whether the quality gate of the data `day` trades on failed: the gate run of the previous
    trading day (the night before). None when that run did not finish or does not exist, so
    entries fail closed."""
    wanted = calendar.previous_trading_day(day) if calendar is not None else None
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
    realised P&L (06 §2) in the bot's mode; free cash is the broker's available funds for a real
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
        virtual = bot_mode(session, bot, at, calendar) == SHADOW
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


class ExitViews:
    """The exits' `ViewSource`: the live price, the sessions held and the daily levels of the
    position's pinned exit rules. None without a live price, and None while the position awaits
    a split or bonus rescale: its prices are on the old scale, so no exit fires from the action
    alone (HAT-EXEC-142); nor ever once it spans an action the rescale cannot handle (a
    consolidation, a rights issue, a demerger): it is manage-only until the owner resolves it
    (HAT-EXEC-144); nor while its instrument has moved to another series (EQ to BE): its prices
    stopped with the old series, so it is manage-only until the owner resolves it
    (HAT-EXEC-146). The price is `LivePrices.price`: one the stale threshold old or outside the
    day's band gives no view, so no exit fires on it (PR 51; the broker stop still rests)."""

    def __init__(
        self,
        book: DailyBook,
        prices: Callable[[Session, uuid.UUID], Decimal | None],
        clock: Clock,
        calendar: CalendarSource,
    ) -> None:
        self._book = book
        self._prices = prices
        self._clock = clock
        self._calendar = calendar

    def __call__(self, session: Session, position: HatPosition) -> ExitView | None:
        price = self._prices(session, position.instrument_id)
        if price is None:
            return None
        today = trade_date_of(utc_now(self._clock))
        if manage_only(session, position, today):
            logger.warning("position %s spans a corporate action not rescaled", position.id)
            return None
        if series_changed(session, position) is not None:
            logger.warning("position %s is on a series its instrument has left", position.id)
            return None
        signal = session.get(HatSignal, position.signal_id)
        if signal is None:
            return None
        calendar = self._calendar()
        trading = calendar.trading_days() if calendar is not None else []
        held = held_sessions(trading, trade_date_of(as_utc(position.opened_at)), today)
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
