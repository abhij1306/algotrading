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
  Fyers' order book can lag a placed order, so a lost day stop or sale is taken as never landed
  only on a second absent read at least `settle_after` after the first (`AbsentReads`): a
  second sell beside one that landed is refused by the broker only when HAT owns every share of
  the symbol, and the owner may hold some (the holding baseline).
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
    "AbsentReads",
    "PositionDesk",
    "Verdict",
    "found_verdict",
    "serialised",
    "second_absence",
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
    from_holdings: bool = False,
) -> Verdict:
    """What a good read of the broker's book (`found`: it holds the order, with `filled_qty`
    shares sold) says about an order whose answer was lost, sent at `placed_at` (None: never
    sent). A sale or an entry (`from_holdings`) of an earlier trade date is STALE, settled from
    the holdings, since a DAY order cannot still rest and the books are today's: they can never
    show it."""
    if found:
        return found_verdict(filled_qty)
    if young(placed_at, now, settle_after):
        return Verdict.WAIT
    if (
        from_holdings
        and placed_at is not None
        and trade_date_of(as_utc(placed_at)) < trade_date_of(now)
    ):
        return Verdict.STALE
    return Verdict.EXPIRE


def second_absence(first_absent: datetime, now: datetime, settle_after: timedelta) -> bool:
    """Whether an absent read at `now` confirms the one at `first_absent`: at least
    `settle_after` later, so a book that lagged the order at the first read has caught up."""
    return now - first_absent >= settle_after


class AbsentReads:
    """When each lost day stop or sale was first read absent from a good read of the order book
    (by its tag). `confirmed` is True only for a second absent read `settle_after` after the
    first; a read that finds the order forgets the first. Held in memory: after a restart the
    first read is taken again, which only waits longer."""

    def __init__(self, settle_after: timedelta) -> None:
        self._settle_after = settle_after
        self._first: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def confirmed(self, tag: str, now: datetime) -> bool:
        with self._lock:
            first = self._first.setdefault(tag, now)
            if not second_absence(first, now, self._settle_after):
                return False
            del self._first[tag]
            return True

    def found(self, tag: str) -> None:
        with self._lock:
            self._first.pop(tag, None)


class PositionDesk:
    """The position locks and the settle wait of one broker account's session. A lost answer is
    settled `settle_after` after its send, by the clock the send was stamped with (`clock`,
    which stamps `placed_at`), not the caller's `now`."""

    def __init__(
        self, *, settle_after: timedelta = LOST_ANSWER_SETTLE, clock: Clock = SYSTEM_CLOCK
    ) -> None:
        self.settle_after = settle_after
        self.clock = clock
        self.absent = AbsentReads(settle_after)
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
        self,
        placed_at: datetime | None,
        found: bool,
        *,
        filled_qty: int = 0,
        from_holdings: bool = False,
        tag: str | None = None,
    ) -> Verdict:
        """`settle_verdict` now. With the order's `tag` (a day stop or sale in the session), an
        EXPIRE stands only on a second absent read (`AbsentReads`); the first is WAIT."""
        now = self.clock.now()
        verdict = settle_verdict(
            placed_at,
            found,
            now,
            self.settle_after,
            filled_qty=filled_qty,
            from_holdings=from_holdings,
        )
        if tag is None:
            return verdict
        if found:
            self.absent.found(tag)
        elif verdict is Verdict.EXPIRE and not self.absent.confirmed(tag, now):
            return Verdict.WAIT
        return verdict


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
