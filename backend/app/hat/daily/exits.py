"""The live exits of the trading day (PR 17): what the exit rules read and the rules of the
virtual positions.

- `ExitViews`: the `ViewSource` of the exits (checked live price, days held, SMA and swing
  lows).
- `ShadowExits`: the live exit rules of every virtual position (a `TradeManager` runs the live
  rules of real positions only), and the 15:20-15:25 rules of the virtual positions of bots whose
  account has no `TradeManager` (its `exits.close` covers its virtual positions too).

Missing data never fires anything: an unknown price leaves the position alone.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import Clock
from ..engine import positions, shadow
from ..engine.close_gap import record_close_evaluation
from ..engine.exits import ExitView, Phase
from ..engine.rescale import manage_only
from ..execution.manage import in_close_window
from ..execution.series import broker_symbol, series_changed
from ..models import HatBot, HatPosition, HatSignal
from ..time import as_utc, trade_date_of, utc_now
from .gates import CalendarSource
from .market import DailyBook, held_sessions

__all__ = ["ExitViews", "ShadowExits"]

logger = logging.getLogger(__name__)


class ExitViews:
    """The exits' `ViewSource`: the live price, the sessions held and the daily levels of the
    position's pinned exit rules. None without a live price, and None while the position awaits
    a split or bonus rescale: its prices are on the old scale, so no exit fires from the action
    alone (HAT-EXEC-142); nor ever once it spans an action the rescale cannot handle (a
    consolidation, a rights issue, a demerger): it is manage-only until the owner resolves it
    (HAT-EXEC-144). A position whose instrument moved to another series (EQ to BE) while it was
    held keeps its exits (D210, HAT-EXEC-146): its prices are continuous under one instrument,
    and a sale goes out under the new series' symbol; only one moved to a series with no Fyers
    symbol (BZ) has none, as no sale can be sent for it. The price is
    `LivePrices.price`: one the stale threshold old or outside the day's band gives no view, so
    no exit fires on it (PR 51; the broker stop still rests)."""

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
        moved = series_changed(session, position)
        if moved is not None and broker_symbol(session, moved.id) is None:
            logger.warning("position %s moved to a series with no Fyers symbol", position.id)
            return None
        signal = session.get(HatSignal, position.signal_id)
        if signal is None:
            return None
        calendar = self._calendar()
        trading = calendar.trading_days() if calendar is not None else []
        held = held_sessions(trading, trade_date_of(as_utc(position.opened_at)), today)
        rules = positions.exit_rules_of(session, signal)
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
                    result = shadow.evaluate_position(session, position.id, view, phase, now)
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
