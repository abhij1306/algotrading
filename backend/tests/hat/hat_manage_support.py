"""One auto-mode bot with a real position, for the trade-management properties (PR 15c).

The router places the entry through the real Fyers adapter over `FakeBroker`; `fill` writes the
broker's trade-book row for it, and `TradeManager.sync` reads it back. Nothing reaches a real
broker.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from hat.fakes.broker import FakeBroker
from hat_engine_support import TRADE_DAY, USER, World, request_for, seed_engine_world
from hat_exec_support import (
    GTT_READY,
    SYMBOLS,
    confirm_gates,
    guard_on,
    live_session,
    seed_live_sessions,
)
from hat_risk_support import CALENDAR
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.engine import trades
from app.hat.execution.fills import FillSync
from app.hat.execution.fyers_adapter import FyersHatBroker
from app.hat.execution.manage import TradeManager
from app.hat.execution.protect import WORKING, StopDesk
from app.hat.execution.reconcile import ReconciliationService, notifier_sink
from app.hat.execution.router import ExecutionRouter, PlacementCheck
from app.hat.models import (
    HatBot,
    HatBrokerAccount,
    HatInstrument,
    HatMarket,
    HatOrder,
    HatPosition,
)
from app.hat.notify import RecordingNotifier
from app.hat.time import IST, utc_now

__all__ = ["TICK", "ManageRig", "at"]

D = Decimal
TICK = D("0.05")
TICKER = "SBIN"
LIVE_LTP = D(10**6)  # a fresh live price above every stop drawn here (PR 51)
LIVE = PlacementCheck(CALENDAR, feed_stale=False, quality_failed=False, gate_on=True, ltp=LIVE_LTP)
FILLED = 2  # Fyers orderbook status of a filled order


def at(hour: int, minute: int) -> datetime:
    """An instant of the trade day, IST."""
    return datetime.combine(TRADE_DAY, time(hour, minute), tzinfo=IST)


class ManageRig:
    """One auto bot on one broker account: the router, the stop desk, the fill sync and the trade
    manager, all over the Fyers adapter and `FakeBroker`. A lost answer is settled at the next
    look (`settle_after` 0) unless a property sets the wait; the settle reads the wall clock
    moved on by `elapsed`. `tick` is the instrument's tick the stop desk reads (a property may
    change it between calls)."""

    def __init__(
        self,
        session: Session,
        *,
        exits: list[dict[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
        ticker: Callable[[], float] | None = None,
        settle_after: timedelta = timedelta(0),
    ) -> None:
        self.session = session
        self.elapsed = timedelta(0)
        self.tick = TICK
        self.world: World = seed_engine_world(session, mode="auto", instruments=1, exits=exits)
        bot = session.get(HatBot, self.world.bot_id)
        assert bot is not None
        self.account_id = bot.broker_account_id
        session.add(
            HatBrokerAccount(
                id=self.account_id,
                user_id=USER,
                broker="fyers",
                account_ref="X0001",
                capabilities=GTT_READY.to_json(),
                status="active",
            )
        )
        session.add(
            HatInstrument(
                id=self.world.instruments[0],
                market_id=session.scalar(select(HatMarket.id)),
                isin="INE000000000",
                series="EQ",
                symbol=TICKER,
                name=TICKER,
                face_value=D(1),
                active=True,
            )
        )
        seed_live_sessions(session, self.account_id)
        session.flush()
        confirm_gates(session, self.account_id, USER)
        session.commit()
        self.fake = FakeBroker()
        broker = FyersHatBroker(
            self.fake,  # type: ignore[arg-type]
            instruments=lambda: list(SYMBOLS.items()),
            capabilities=GTT_READY,
        )
        live_session(SimpleNamespace(broker=broker))  # type: ignore[arg-type]
        self.broker = broker
        self.guard = guard_on(TRADE_DAY)
        self.notifier = RecordingNotifier()
        self.reconciler = ReconciliationService(broker, self.guard, notifier_sink(self.notifier))
        self.router = ExecutionRouter(broker, self.guard, self.reconciler, self.notifier)
        self.desk = StopDesk(
            broker,
            self.guard,
            tick_for=lambda _session, _position: self.tick,
            calendar=CALENDAR,
            notifier=self.notifier,
            settle_after=settle_after,
            wall=self.wall,
        )
        self.fills = FillSync(broker, self.desk, self.notifier)
        self.manager = TradeManager(
            broker,
            self.guard,
            self.desk,
            self.fills,
            calendar=CALENDAR,
            notifier=self.notifier,
            clock=clock,
            settle_after=settle_after,
            wall=self.wall,
            **({"ticker": ticker} if ticker is not None else {}),
        )
        self._trades = 0

    def wall(self) -> datetime:
        return utc_now() + self.elapsed

    # --- the entry and its fills ----------------------------------------------------------------

    def enter(self, when: datetime) -> trades.EntryOutcome:
        request = request_for(self.world, self.world.instruments[0])
        outcome = self.router.route(
            self.session,
            trades.EntryRequest(**{**vars(request), "triggered_at": when}),
            now=when,
            check=LIVE,
            confidence="high",
        )
        assert self.entry_row(outcome.signal_id) is not None, "auto placed the entry"
        return outcome

    def entry_row(self, signal_id: uuid.UUID) -> HatOrder | None:
        return self.session.scalar(
            select(HatOrder).where(HatOrder.signal_id == signal_id, HatOrder.purpose == "entry")
        )

    def fill(self, row: HatOrder, qty: int, price: Decimal) -> None:
        """The broker filled `qty` more of `row` at `price` (trade book and order book)."""
        self._trades += 1
        side = -1 if row.purpose != "entry" else 1
        self.fake.tradebook = [  # type: ignore[union-attr]
            *self.fake.tradebook,  # type: ignore[misc]
            {
                "tradeNumber": f"T-{self._trades}",
                "orderNumber": row.broker_order_id,
                "orderTag": row.tag,
                "symbol": SYMBOLS[TICKER],
                "side": side,
                "tradedQty": qty,
                "tradePrice": float(price),
            },
        ]
        for book in self.fake.book:
            if book["orderTag"] == row.tag:
                book["filledQty"] += qty
                if book["filledQty"] >= book["qty"]:
                    book["status"] = FILLED

    def sync(self, when: datetime) -> None:
        self.manager.sync(self.session, self.account_id, when)

    # --- reading --------------------------------------------------------------------------------

    def position(self, signal_id: uuid.UUID) -> HatPosition | None:
        row = self.session.scalar(
            select(HatPosition).where(
                HatPosition.signal_id == signal_id, HatPosition.is_virtual.is_(False)
            )
        )
        if row is not None:
            self.session.refresh(row)
        return row

    def stops(self, position_id: uuid.UUID, *statuses: str) -> list[HatOrder]:
        rows = StopDesk.stops_of(self.session, position_id, *(statuses or WORKING))
        for row in rows:
            self.session.refresh(row)
        return rows

    def codes(self) -> list[str]:
        return [code for code, _, _ in self.notifier.alerts]

    def call_names(self) -> list[str]:
        return [name for name, _ in self.fake.calls]
