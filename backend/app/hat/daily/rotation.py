"""The rotation job of the trading day: `rotation.rebalance` at 09:20 (`engine/rotation_live.py`).

For each active bot whose live config is a rotation: the sales an earlier job left waiting for a
price, then the rebalance its last rebalance night recorded for today, on paper, on the live
prices of the session (the instruments are priced and subscribed from `live.prepare`,
`planned_instruments`). The entry context is the trigger service's (`LiveContexts`): the sleeve's
equity and free cash, the kills, the feed and the quality gate. A bot that asks for confirm or
auto does not trade at all (real rotation orders are not built, and its entry context would be the
real book's): its job is recorded `real_orders_not_built`, its waiting sales wait on, and the
owner is told (`rotation.shadow_only`); a rebalance that could not size (no allocation for the
night) is told too (`rotation.skipped`). Each bot is its own transaction, and a bot whose job
already ran today (`rotation.rebalanced`) is not run again.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy.orm import Session

from ..clock import Clock
from ..configs.records import Bot
from ..configs.unit import configs_of
from ..engine.rotation_live import (
    Planned,
    Rebalanced,
    pending_sales,
    planned,
    rebalance,
    rebalanced_on,
    record_rebalanced,
    rotation_config,
    waiting_sales,
)
from ..execution.modes import bot_mode
from ..governance import Mode
from ..notify import Notifier
from ..ops.jobs import JobResult, SessionFactory
from ..time import utc_now
from .entries import LiveContexts
from .gates import CalendarSource
from .prices import LivePrices

__all__ = ["ALERT_SHADOW_ONLY", "ALERT_SKIPPED", "REBALANCE_AT", "ROTATION_REBALANCE", "Rotations"]

ROTATION_REBALANCE = "rotation.rebalance"
# The first final 5-minute bar and the regime gate (`GateBoard`): the open's prices are in.
REBALANCE_AT = time(9, 20)
ALERT_SHADOW_ONLY = "rotation.shadow_only"
ALERT_SKIPPED = "rotation.skipped"
REAL_NOT_BUILT = "real_orders_not_built"


class Rotations:
    """The `rotation.rebalance` job body over the live objects of one process."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        clock: Clock,
        contexts: LiveContexts,
        prices: LivePrices,
        calendar: CalendarSource,
        notifier: Notifier,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._contexts = contexts
        self._prices = prices
        self._calendar = calendar
        self._notifier = notifier

    def rebalance(self, day: date) -> JobResult:
        with self._session_factory() as session:
            bots = [
                b.id
                for b in configs_of(session).all(
                    Bot, lambda b: b.status == "active" and b.live_version_id is not None
                )
                if rotation_config(session, b) is not None
            ]
        rows = 0
        for bot_id in bots:
            rows += self._one(bot_id, day)
        return JobResult(rows=rows)

    def _one(self, bot_id: uuid.UUID, day: date) -> int:
        now = utc_now(self._clock)
        with self._session_factory() as session:
            bot = configs_of(session).get(Bot, bot_id)
            if bot is None or rebalanced_on(session, bot_id, day):
                return 0
            asked = bot_mode(session, bot, now, self._calendar())
            done = Rebalanced(day)
            waiting = waiting_sales(session, bot_id)
            night = planned(session, bot_id, day)
            if night is None and not waiting:
                return 0
            if asked != Mode.SHADOW:
                # its entry context would be the real book's: nothing trades, and the waiting
                # sales wait on
                done.skipped = REAL_NOT_BUILT
                done.waiting.update(waiting)
            else:
                self._trade(session, bot_id, night, waiting, now, done)
            record_rebalanced(session, bot_id, done, now, asked=asked)
            session.commit()
        self._tell(bot_id, day, asked, done)
        return len(done.rows)

    def _trade(
        self,
        session: Session,
        bot_id: uuid.UUID,
        night: Planned | None,
        waiting: dict[str, str],
        now: datetime,
        done: Rebalanced,
    ) -> None:
        def price(name: str) -> Decimal | None:
            return self._prices.checked(session, uuid.UUID(name), now)[0]

        if waiting:
            pending_sales(session, bot_id, waiting, price, now, done)
        if night is None:
            return
        traded = rebalance(
            session,
            bot_id,
            night,
            price=price,
            context=lambda: self._contexts.entry_context(session, bot_id, now),
            now=now,
        )
        done.rows.extend(traded.rows)
        done.waiting.update(traded.waiting)
        done.skipped = traded.skipped

    def _tell(self, bot_id: uuid.UUID, day: date, asked: str, done: Rebalanced) -> None:
        if asked != Mode.SHADOW:
            self._notifier.alert(ALERT_SHADOW_ONLY, day, {"bot_id": str(bot_id), "asked": asked})
        elif done.skipped is not None:
            self._notifier.alert(
                ALERT_SKIPPED, day, {"bot_id": str(bot_id), "reason": done.skipped}
            )
