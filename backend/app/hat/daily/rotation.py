"""The rotation job of the trading day: `rotation.rebalance` at 09:20 (`engine/rotation_live.py`).

For each active bot whose live config is a rotation: the sales an earlier job left waiting for a
price, then the mid-period scale-down its last night recorded for today (`rotation.scale_down`,
only with the rules' `scale_down`), then the rebalance its last rebalance night recorded for
today, on paper, on the live
prices of the session (the instruments are priced and subscribed from `live.prepare`,
`planned_instruments`). The entry context is the trigger service's (`LiveContexts`): the sleeve's
equity and free cash, the kills, the feed and the quality gate. A bot that asks for confirm or
auto does not trade at all (real rotation orders are not built, and its entry context would be the
real book's): its job is recorded `real_orders_not_built`, its waiting sales wait on, and the
owner is told (`rotation.shadow_only`); a rebalance that could not size (no allocation for the
night) is told too (`rotation.skipped`), and so is every buy not made (`rotation.buy_refused`).
A rebalance that reaches a name it may buy with no tick yet, or a stale one, or runs on a stale
feed, writes nothing for its bot (no lower-ranked name takes its place) until `PRICES_WAIT_UNTIL`
(09:45, with the open): the job fails `rotation_unpriced` and runs again each minute
(`rotation.unpriced`, told once). After it such a name is passed over as the backtest passes over
a name with no bar, the rest trades, and `rotation.unpriced` names what was skipped. Each bot is
its own transaction, and a bot whose job already ran today (`rotation.rebalanced`) is not run
again.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from ..clock import Clock
from ..configs.records import Bot
from ..configs.unit import configs_of
from ..engine.rotation_live import (
    Planned,
    Rebalanced,
    RotationUnpricedError,
    ScaleDownPlanned,
    pending_sales,
    planned,
    rebalance,
    rebalanced_on,
    record_rebalanced,
    rotation_config,
    scale_down,
    scale_down_planned,
    waiting_sales,
)
from ..execution.modes import bot_mode
from ..governance import Mode
from ..notify import Notifier
from ..ops.jobs import JobError, JobResult, SessionFactory
from ..time import IST, utc_now
from .entries import LiveContexts
from .gates import CalendarSource
from .prices import LivePrices

__all__ = [
    "ALERT_BUY_REFUSED",
    "ALERT_SHADOW_ONLY",
    "ALERT_SKIPPED",
    "ALERT_UNPRICED",
    "PRICES_WAIT_UNTIL",
    "REBALANCE_AT",
    "REBALANCE_UNTIL",
    "ROTATION_REBALANCE",
    "Rotations",
]

ROTATION_REBALANCE = "rotation.rebalance"
# The first final 5-minute bar and the regime gate (`GateBoard`): the open's prices are in.
REBALANCE_AT = time(9, 20)
ALERT_SHADOW_ONLY = "rotation.shadow_only"
ALERT_SKIPPED = "rotation.skipped"
ALERT_UNPRICED = "rotation.unpriced"
ALERT_BUY_REFUSED = "rotation.buy_refused"
UNPRICED = "rotation_unpriced"
# A rebalance waits for its names' first ticks until then (moved with the session's open); after
# it a name still unpriced is passed over as a name with no bar and the rest trades. The job's
# window ends a little later, so its first run after the cutoff trades.
PRICES_WAIT_UNTIL = time(9, 45)
REBALANCE_UNTIL = time(10, 0)
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
        self._told: set[tuple[uuid.UUID, date]] = set()  # `rotation.unpriced` told

    def rebalance(self, day: date) -> JobResult:
        """Every rotation bot's job for `day`. A bot whose rebalance found a name it may buy
        without a live price yet wrote nothing; once the others ran the job fails
        (`rotation_unpriced`) and the scheduler runs it again until its window ends, the owner
        told once a day per bot (`rotation.unpriced`)."""
        with self._session_factory() as session:
            bots = [
                b.id
                for b in configs_of(session).all(
                    Bot, lambda b: b.status == "active" and b.live_version_id is not None
                )
                if rotation_config(session, b) is not None
            ]
        rows = 0
        waiting: list[uuid.UUID] = []
        for bot_id in bots:
            try:
                rows += self._one(bot_id, day)
            except RotationUnpricedError as unpriced:
                waiting.append(bot_id)
                if (bot_id, day) not in self._told:
                    self._told.add((bot_id, day))
                    self._notifier.alert(
                        ALERT_UNPRICED,
                        day,
                        {"bot_id": str(bot_id), "names": ",".join(unpriced.names)[:200]},
                    )
        if waiting:
            raise JobError(UNPRICED)
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
            trim = scale_down_planned(session, bot_id, day)
            if night is None and trim is None and not waiting:
                return 0
            if asked != Mode.SHADOW:
                # its entry context would be the real book's: nothing trades, and the waiting
                # sales wait on
                done.skipped = REAL_NOT_BUILT
                done.waiting.update(waiting)
            else:
                # an unpriced name raises before the commit: this bot's writes roll back
                self._trade(session, bot_id, night, trim, waiting, now, done)
            record_rebalanced(session, bot_id, done, now, asked=asked)
            session.commit()
        self._tell(bot_id, day, asked, done)
        return len(done.rows)

    def _trade(
        self,
        session: Session,
        bot_id: uuid.UUID,
        night: Planned | None,
        trim: ScaleDownPlanned | None,
        waiting: dict[str, str],
        now: datetime,
        done: Rebalanced,
    ) -> None:
        def price(name: str) -> tuple[Decimal | None, str | None]:
            return self._prices.checked(session, uuid.UUID(name), now)

        if trim is not None:
            # before the waiting sales: an unpriced holding raises before anything is written
            scale_down(
                session,
                bot_id,
                trim,
                price=price,
                waiting=waiting,
                now=now,
                wait_until=self._wait_until(trim.trade_date),
                done=done,
            )
        if waiting:
            pending_sales(session, bot_id, waiting, price, now, done)
        if night is None:
            return
        done.absorb(
            rebalance(
                session,
                bot_id,
                night,
                price=price,
                context=lambda: self._contexts.entry_context(session, bot_id, now),
                now=now,
                wait_until=self._wait_until(night.trade_date),
            )
        )

    def _wait_until(self, day: date) -> datetime:
        """`PRICES_WAIT_UNTIL` on `day`, moved with its session's open."""
        calendar = self._calendar()
        shift = calendar.open_shift(day) if calendar is not None else timedelta(0)
        return datetime.combine(day, PRICES_WAIT_UNTIL, tzinfo=IST) + shift

    def _tell(self, bot_id: uuid.UUID, day: date, asked: str, done: Rebalanced) -> None:
        bot = str(bot_id)
        if asked != Mode.SHADOW:
            self._notifier.alert(ALERT_SHADOW_ONLY, day, {"bot_id": bot, "asked": asked})
            return
        if done.skipped is not None:
            self._notifier.alert(ALERT_SKIPPED, day, {"bot_id": bot, "reason": done.skipped})
        if done.unpriced:
            self._notifier.alert(
                ALERT_UNPRICED,
                day,
                {"bot_id": bot, "skipped": ",".join(done.unpriced)[:200], "traded": "rest"},
            )
        if done.refused:
            refused = ",".join(f"{name}:{why}" for name, why in sorted(done.refused.items()))
            self._notifier.alert(
                ALERT_BUY_REFUSED,
                day,
                {"bot_id": bot, "count": len(done.refused), "buys": refused[:200]},
            )
