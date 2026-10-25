"""The live exits of the trading day (PR 17): what the exit rules read and the rules of the
virtual positions.

- `ExitViews`: the `ViewSource` of the exits (checked live price, days held, SMA and swing
  lows). A view that raises raises URGENT `exits.view_failed` once per position and trade date:
  that position's exit rules are off (only its resting broker stop protects it), and each loop
  goes on to the next position.
- `ShadowExits`: the live exit rules of every virtual position (a `TradeManager` runs the live
  rules of real positions only), and the 15:20-15:25 rules of the virtual positions of bots whose
  account has no `TradeManager` (its `exits.close` covers its virtual positions too). That close
  pass first closes every virtual position whose demerger exit falls on the day (CA8,
  `engine/demerger_exit.py`), whatever its account.

Missing data never fires anything: an unknown price leaves the position alone.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import Clock
from ..engine import demerger_exit, positions, shadow
from ..engine.close_gap import record_close_evaluation
from ..engine.exits import ExitView, Phase
from ..engine.rescale import actions_between, flag_split_gap, manage_only, split_like
from ..execution.manage import in_close_window
from ..execution.series import broker_symbol, series_changed
from ..models import HatBot, HatPosition, HatSignal
from ..notify import Notifier
from ..time import as_utc, trade_date_of, utc_now
from .gates import CalendarSource
from .market import DailyBook, held_sessions

__all__ = ["VIEW_FAILED", "ExitViews", "ShadowExits"]

logger = logging.getLogger(__name__)

VIEW_FAILED = "exits.view_failed"


class ExitViews:
    """The exits' `ViewSource`: the live price, the sessions held and the daily levels of the
    position's pinned exit rules. None without a live price, and None while the position awaits
    a split or bonus rescale: its prices are on the old scale, so no exit fires from the action
    alone (HAT-EXEC-142); nor ever once it spans an action the rescale cannot handle (a
    consolidation, a rights issue, a demerger): it is manage-only until the owner resolves it
    (HAT-EXEC-144). A position whose instrument moved to another series (EQ to BE) while it was
    held keeps its exits (D210, HAT-EXEC-146): its prices are continuous under one instrument,
    and a sale goes out under the new series' symbol; only one moved to a series with no Fyers
    symbol (BZ) has none, as no sale can be sent for it. A live price at or below 0.6 of the
    previous raw close with no action stored since is an unstored split (Q3): the position
    gets one URGENT `position.split_gap`, in its own transaction, and no view until the owner
    stores the action or resolves the gap (`rescale.open_guards`). The price is
    `LivePrices.exit_price`: one outside the day's band gives no view, nor one the stale threshold
    old unless the feed itself is fresh (an illiquid name keeps its last trade of the day, QA15),
    so no exit fires on it (PR 51; the broker stop still rests)."""

    def __init__(
        self,
        book: DailyBook,
        prices: Callable[[Session, uuid.UUID], Decimal | None],
        clock: Clock,
        calendar: CalendarSource,
        session_factory: Callable[[], Session],
        notifier: Notifier,
    ) -> None:
        self._book = book
        self._prices = prices
        self._clock = clock
        self._calendar = calendar
        self._session_factory = session_factory
        self._notifier = notifier
        self._flag_lock = threading.Lock()  # the shadow and account exits share the views
        self._failed_lock = threading.Lock()
        self._failed: dict[uuid.UUID, date] = {}  # the trade date of each view_failed raised

    def price(self, session: Session, instrument_id: uuid.UUID) -> Decimal | None:
        """The instrument's checked exit price (`LivePrices.exit_price`); None when unknown."""
        return self._prices(session, instrument_id)

    def trading_days(self) -> list[date]:
        """The calendar's trading days, sorted; none without a calendar."""
        calendar = self._calendar()
        return sorted(calendar.trading_days()) if calendar is not None else []

    def __call__(self, session: Session, position: HatPosition) -> ExitView | None:
        try:
            return self._view(session, position)
        except Exception:
            self._view_failed(position)
            raise

    def _view_failed(self, position: HatPosition) -> None:
        """Raise `exits.view_failed` for the position once per trade date (again on the next
        failure when it could not be delivered)."""
        today = trade_date_of(utc_now(self._clock))
        with self._failed_lock:
            if self._failed.get(position.id) == today:
                return
            self._failed[position.id] = today
        kind = "shadow" if position.is_virtual else "real"
        try:
            sent = self._notifier.alert(
                VIEW_FAILED, today, {"position": str(position.id), "kind": kind}
            )
        except Exception:
            logger.exception("alert %s for position %s not sent", VIEW_FAILED, position.id)
            sent = False
        if not sent:
            with self._failed_lock:
                self._failed.pop(position.id, None)

    def _view(self, session: Session, position: HatPosition) -> ExitView | None:
        price = self._prices(session, position.instrument_id)
        if price is None:
            return None
        today = trade_date_of(utc_now(self._clock))
        if manage_only(session, position, today):
            logger.warning("position %s spans a corporate action not rescaled", position.id)
            return None
        if self._split_gap(session, position, price, today):
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

    def _split_gap(
        self, session: Session, position: HatPosition, price: Decimal, today: date
    ) -> bool:
        """True, after recording `position.split_gap` in a transaction of its own, when the
        live price gaps split-like from the previous raw close with no action stored since."""
        previous = self._book.day(today).previous_raw_close(position.instrument_id)
        if previous is None or not split_like(price, previous[1]):
            return False
        if actions_between(session, position.instrument_id, previous[0], today):
            return False
        logger.warning("position %s gaps like an unstored split", position.id)
        try:
            with self._flag_lock, self._session_factory() as own:
                held = own.get(HatPosition, position.id)
                if held is not None and held.closed_at is None:
                    gap = (previous[0], previous[1], price)
                    flag_split_gap(own, held, today, gap, utc_now(self._clock))
                    own.commit()
        except Exception:
            logger.exception("recording the split gap of position %s failed", position.id)
        return True


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
                try:
                    view = self._views(session, position)
                    if view is None or view.price is None:
                        continue
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

    def demergers(self, now: datetime) -> int:
        """Close each open virtual position whose demerger exit is today at the live price
        (CA8); one without a price stays open, and the nightly alerts it late. Returns how many
        were closed."""
        today = trade_date_of(now)
        closed = 0
        with self._session_factory() as session:
            due = [
                d
                for d in demerger_exit.due_positions(session, self._views.trading_days())
                if d.exit_on == today and d.position.is_virtual
            ]
            for d in due:
                price = self._views.price(session, d.position.instrument_id)
                if price is None:
                    logger.warning("no price for the demerger exit of position %s", d.position.id)
                    continue
                try:
                    closed += demerger_exit.close_due(session, d.position, price, now)
                    session.commit()
                except Exception:
                    session.rollback()
                    logger.exception("demerger exit of position %s failed", d.position.id)
        return closed

    def close(self, now: datetime, shift: timedelta = timedelta(0)) -> int:
        """The close-phase pass (the day's demerger exits first); nothing outside the close
        window (15:20-15:25 IST in a regular session, moved by the session's `shift` from the
        regular close, `in_close_window`)."""
        if not in_close_window(now, shift):
            return 0
        return self.demergers(now) + self.run(now, "close")
