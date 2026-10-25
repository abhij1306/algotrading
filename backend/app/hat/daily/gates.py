"""The entry gates of the trading day (PR 17): each bot's regime gate and whether last night's
quality gate passed for the data the day trades on.

`GateBoard` evaluates each bot's regime gate at or after 09:20 IST of the trade date (moved with a
session that opens at another time, `SessionCalendar.open_shift`) and keeps it for the day; before
then it is unknown, so no entry is accepted (HAT-RISK-051). An unknown gate
or quality state leaves the entry alone (the risk check rejects an entry with a missing input).
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from ..data.quality import gate_failed
from ..data.store import DataStore
from ..engine.regime_gate import regime_allows_longs
from ..engine.strategy.config import parse_config
from ..models import HatBot, HatStrategyVersion
from ..risk.sessions import SessionCalendar
from ..time import IST, trade_date_of
from .market import index_closes, sessions_before

__all__ = ["GATE_AT", "CalendarSource", "GateBoard"]

logger = logging.getLogger(__name__)

GATE_AT = time(9, 20)  # the first 5-minute bar is final; the gate is evaluated again from here

# The trading calendar the runtime keeps (read again every 10 minutes), None while unknown.
CalendarSource = Callable[[], SessionCalendar | None]


class GateBoard:
    """Each bot's regime gate for the trade date, evaluated once at or after 09:20 IST on the
    index closes before the trade date (HAT-RISK-051), and the quality-gate state of the day."""

    def __init__(self, store: DataStore, calendar: CalendarSource | None = None) -> None:
        self._store = store
        self._calendar = calendar
        self._gates: dict[tuple[uuid.UUID, date], bool] = {}
        self._lock = threading.Lock()

    def gate_on(self, session: Session, bot_id: uuid.UUID, at: datetime) -> bool | None:
        day = trade_date_of(at)
        calendar = self._calendar() if self._calendar is not None else None
        shift = calendar.open_shift(day) if calendar is not None else timedelta(0)
        if (at - shift).astimezone(IST).time() < GATE_AT:
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
        """Whether the quality gate of the data `day` trades on failed: the gate run of the
        previous trading day (the night before). None when that run did not finish or does not
        exist, so entries fail closed."""
        wanted = calendar.previous_trading_day(day) if calendar is not None else None
        return None if wanted is None else gate_failed(session, wanted)
