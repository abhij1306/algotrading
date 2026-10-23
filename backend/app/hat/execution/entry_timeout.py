"""Unfilled entries that should not rest any longer (HAT-EXEC-046; PR 49).

A real entry is a DAY LIMIT at the larger of the level and the trigger bar's close (HAT-CRIT-119).
Left alone it rests until the close and fills only when the price comes back down to it, which on
a failed break is exactly the trade not wanted. `EntryWatch` decides when an entry nothing of
which has filled is cancelled:

- **The break failed:** a final 5-minute bar of the instrument, later than the trigger bar,
  closed below the signal's level.
- **It waited too long:** `HAT_ENTRY_TIMEOUT_MINUTES` (default 60) have passed since it was
  placed.

The cancel is `EntryCancel.cancel_unfilled`, the path that cancels an entry remainder, so it
is sent once per entry and the signal ends Cancelled when the fill sync sees the order end
unfilled. An entry with any fill keeps its remainder working (the partial-entry policy). Shadow
entries fill at once, so only real ones are watched.

The closes come from the same final bars the trigger monitor reads (`LiveData` bar listener),
so they share the feed's one dated tick input (PR 51). They are kept in memory: a restart forgets
the bars before it, and only the timeout and later bars end such an entry.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.bars import Bar
from ..engine import lifecycle
from ..models import HatBot, HatInstrument, HatOrder, HatSignal
from ..time import as_utc, trade_date_of
from .submit import ENTRY

__all__ = [
    "BREAK_FAILED",
    "TIMED_OUT",
    "EntryWatch",
    "cancel_reason",
]

logger = logging.getLogger(__name__)

BREAK_FAILED = "break_failed"
TIMED_OUT = "entry_timeout"
WORKING = "placed"  # at the broker with nothing filled (a part fill is `partially_filled`)


def cancel_reason(
    *,
    level: Decimal,
    triggered_at: datetime,
    placed_at: datetime,
    closes: Iterable[tuple[datetime, Decimal]],
    now: datetime,
    timeout: timedelta,
) -> str | None:
    """Why an unfilled entry is cancelled at `now`, or None while it may rest: a final bar after
    the trigger bar (final instant after `triggered_at`) closed below `level`, or `timeout` has
    passed since `placed_at`. `closes` are the (final instant, close) pairs seen so far."""
    if any(triggered_at < at and close < level for at, close in closes):
        return BREAK_FAILED
    if now - placed_at >= timeout:
        return TIMED_OUT
    return None


class EntryWatch:
    """The day's final bar closes per symbol (a bar listener, on the loop thread), and the
    real unfilled entries of an account due a cancel (`due`, on a worker thread)."""

    def __init__(self, timeout: timedelta) -> None:
        self.timeout = timeout
        self._closes: dict[str, list[tuple[datetime, Decimal]]] = {}
        self._day: date | None = None
        self._lock = threading.Lock()

    def on_bar(self, bar: Bar) -> None:
        """Keep a final bar's close; a new trade date drops the last one's."""
        day = trade_date_of(bar.final_at)
        with self._lock:
            if day != self._day:
                self._closes.clear()
                self._day = day
            self._closes.setdefault(bar.symbol, []).append((bar.final_at, bar.close))

    def closes(self, symbol: str) -> list[tuple[datetime, Decimal]]:
        with self._lock:
            return list(self._closes.get(symbol, ()))

    def due(
        self, session: Session, account_id: uuid.UUID, now: datetime
    ) -> list[tuple[uuid.UUID, str]]:
        """(signal id, reason) of each of the account's entries of today working at the broker
        with nothing filled (the signal still EntryPlaced: its first fill opens it, and a virtual
        entry fills at once; the order `placed`, not part filled) that `cancel_reason` ends at
        `now`. The timeout runs from the working order's placement, not from an earlier attempt
        the broker rejected. The desk cancels only real rows at the broker."""
        rows = session.execute(
            select(HatSignal, HatOrder.placed_at, HatInstrument.symbol)
            .join(HatBot, HatBot.id == HatSignal.bot_id)
            .join(HatOrder, HatOrder.signal_id == HatSignal.id)
            .join(HatInstrument, HatInstrument.id == HatSignal.instrument_id)
            .where(
                HatBot.broker_account_id == account_id,
                HatSignal.state == lifecycle.ENTRY_PLACED,
                HatSignal.trade_date == trade_date_of(now),
                HatOrder.purpose == ENTRY,
                HatOrder.status == WORKING,
                HatOrder.placed_at.is_not(None),
            )
            .order_by(HatSignal.id)
        ).all()
        found: list[tuple[uuid.UUID, str]] = []
        # one working entry per signal: the retry is sent only after a rejection
        for signal, placed_at, symbol in rows:
            reason = cancel_reason(
                level=Decimal(signal.trigger_price),
                triggered_at=as_utc(signal.triggered_at),
                placed_at=as_utc(placed_at),
                closes=self.closes(symbol),
                now=now,
                timeout=self.timeout,
            )
            if reason is not None:
                found.append((signal.id, reason))
        return found
