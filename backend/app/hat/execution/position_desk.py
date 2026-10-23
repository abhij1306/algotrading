"""Per-position concurrency and lost-answer settlement for real positions (HAT-EXEC-065).

`PositionDesk` is shared by an account's `StopDesk` and `TradeManager`:

- **One write lock per position** (`lock`): every path that writes to the broker about a
  position holds it, the stop paths (protect, resize, follow, make_room, cancel, swap, verify,
  the GTT check) and the sale path, from its "nothing else is selling" check until the sale is
  sent. The fill sync, the live tick path, the jobs and the Telegram flatten run on their own
  threads over one desk; without the one lock, `make_room` could cancel a stop for a sale and
  the fill sync, seeing no stop and no live sale yet, place a new one under it, which makes the
  broker refuse the sale. The lock is re-entrant: the sale path calls `protect` while it holds
  it (no stop path ever calls a sale). Nothing may enter it with an uncommitted write: another
  thread holding it could wait on that row.
- **One settle rule** (`settle_verdict`): an order whose answer was lost, against a good read of
  the broker's book. One the book shows with shares already sold waits for the fill sync
  (`found_verdict`), which adopts it with its fills: adopted bare, a stop or sale would leave
  shares HAT still counts with no stop over them, and a new stop would go over shares sold.
"""

from __future__ import annotations

import functools
import logging
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, TypeVar

from ..clock import SYSTEM_CLOCK, Clock
from ..time import as_utc, trade_date_of

__all__ = [
    "LOST_ANSWER_SETTLE",
    "PositionDesk",
    "Verdict",
    "found_verdict",
    "serialised",
    "settle_verdict",
]

logger = logging.getLogger(__name__)

# How long after its send an order whose answer was lost is settled against the broker's books:
# the guarded call gives up after 1.5 s, but the SDK request may still be in flight and land.
LOST_ANSWER_SETTLE = timedelta(seconds=60)


class Verdict(StrEnum):
    ADOPT = "adopt"  # the book holds it: record what the broker shows
    EXPIRE = "expire"  # absent after a good read: it never landed
    WAIT = "wait"  # absent, but sent too recently: it may still land
    STALE = "stale"  # a sale of an earlier session, absent: today's book cannot show it


def young(placed_at: datetime | None, now: datetime, settle_after: timedelta) -> bool:
    """Sent less than `settle_after` before `now`: its answer may still be on its way."""
    return placed_at is not None and now - as_utc(placed_at) < settle_after


def found_verdict(filled_qty: int) -> Verdict:
    """A stop or sale whose answer was lost, found in the broker's book with `filled_qty` shares
    sold: adopted when none sold; else WAIT, for the fill sync to adopt it with its fills."""
    return Verdict.WAIT if filled_qty > 0 else Verdict.ADOPT


def settle_verdict(
    placed_at: datetime | None,
    found: bool,
    now: datetime,
    settle_after: timedelta,
    *,
    filled_qty: int = 0,
    sale: bool = False,
) -> Verdict:
    """What a good read of the broker's book (`found`: it holds the order, with `filled_qty`
    shares sold) says about a stop or sale whose answer was lost, sent at `placed_at` (None:
    never sent). A sale (`sale`) of an earlier trade date is STALE, settled from the holdings,
    since a DAY market sale cannot still rest and the order book is today's."""
    if found:
        return found_verdict(filled_qty)
    if young(placed_at, now, settle_after):
        return Verdict.WAIT
    if sale and placed_at is not None and trade_date_of(as_utc(placed_at)) < trade_date_of(now):
        return Verdict.STALE
    return Verdict.EXPIRE


class PositionDesk:
    """The position locks and the settle wait of one broker account's session. A lost answer is
    settled `settle_after` after its send, by the clock the send was stamped with (`clock`,
    which stamps `placed_at`), not the caller's `now`."""

    def __init__(
        self, *, settle_after: timedelta = LOST_ANSWER_SETTLE, clock: Clock = SYSTEM_CLOCK
    ) -> None:
        self.settle_after = settle_after
        self.clock = clock
        self._locks: dict[uuid.UUID, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    @contextmanager
    def lock(self, position_id: uuid.UUID, holder: str) -> Iterator[None]:
        """Hold the position's one re-entrant write lock for `holder` (the step, for the log); a
        step that finds it held by another thread waits for it, and says so."""
        with self._locks_guard:
            lock = self._locks.setdefault(position_id, threading.RLock())
        if not lock.acquire(blocking=False):
            logger.info("position %s busy: %s waits for its lock", position_id, holder)
            lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def young(self, placed_at: datetime | None) -> bool:
        """Sent less than `settle_after` ago: its answer may still be on its way."""
        return young(placed_at, self.clock.now(), self.settle_after)

    def verdict(
        self, placed_at: datetime | None, found: bool, *, filled_qty: int = 0, sale: bool = False
    ) -> Verdict:
        """`settle_verdict` now."""
        return settle_verdict(
            placed_at, found, self.clock.now(), self.settle_after, filled_qty=filled_qty, sale=sale
        )


class _Positioned(Protocol):
    @property
    def positions(self) -> PositionDesk: ...


_R = TypeVar("_R")


def serialised(method: Callable[..., _R]) -> Callable[..., _R]:
    """Hold the position's write lock (`PositionDesk.lock`) for a method called as
    `(self, session, position_id, ...)` on an object with the account's `positions`. The fill
    sync, the sales and the jobs run on their own threads over one desk: without it, two could
    see no working stop and each place one, or one place a stop while a sale it made room for is
    sent."""

    @functools.wraps(method)
    def locked(
        self: _Positioned, session: object, position_id: uuid.UUID, *args: object, **kwargs: object
    ) -> _R:
        with self.positions.lock(position_id, method.__name__):
            return method(self, session, position_id, *args, **kwargs)

    return locked
