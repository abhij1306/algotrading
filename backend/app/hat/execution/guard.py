"""What the order path reads before it places anything (HAT-BRK-110, -111; D118).

The reconciliation gate is keyed by trade date: a success on one trade date never opens the next
one's entries. It gates new entries only: a stop or a sale goes out whatever the reconciliation
state, and a mismatch blocks the entries of the bot or account it is about.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from ..time import trade_date_of, utc_now

__all__ = ["ExecutionGuard"]


def _today() -> date:
    return trade_date_of(utc_now())


@dataclass
class ExecutionGuard:
    """What the order path reads: the trade date last reconciled, and who is blocked."""

    reconciled_for: date | None = None  # the trade date of the last successful reconciliation
    blocked_bots: set[uuid.UUID] = field(default_factory=set)
    blocked_accounts: set[uuid.UUID] = field(default_factory=set)
    today: Callable[[], date] = field(default=_today, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _reads: int = field(default=0, repr=False)  # book reads begun
    _applied: int = field(default=0, repr=False)  # the latest read whose result is in force

    def begin_read(self) -> int:
        """A ticket for a reconciliation about to read the broker's book."""
        with self._lock:
            self._reads += 1
            return self._reads

    def update(
        self,
        *,
        read: int,
        day: date,
        blocked_bots: set[uuid.UUID],
        blocked_accounts: set[uuid.UUID],
    ) -> None:
        """A successful reconciliation of a book read on trade date `day`: that date's entries
        open, minus the blocked ones. A run that read the book before the one in force (two runs
        on different threads finishing out of order) changes nothing: an older clean book never
        clears a newer mismatch."""
        with self._lock:
            if read <= self._applied:
                return
            self._applied = read
            self.reconciled_for = day
            self.blocked_bots = set(blocked_bots)
            self.blocked_accounts = set(blocked_accounts)

    def reconciled_today(self) -> bool:
        return self.reconciled_for == self.today()

    def entry_blocked(self, bot_id: uuid.UUID, account_id: uuid.UUID) -> bool:
        return bot_id in self.blocked_bots or account_id in self.blocked_accounts

    def refusal(self, bot_id: uuid.UUID, account_id: uuid.UUID, *, is_entry: bool) -> str | None:
        """Why an order may not go to the broker now (a fixed code), or None. An entry waits for
        today's reconciliation and stops while the bot or account is blocked; a stop or a sale
        is never refused here (trading-safety rules 5 and 7)."""
        if not is_entry:
            return None
        with self._lock:
            if self.reconciled_for != self.today():
                return "not_reconciled"
            if self.entry_blocked(bot_id, account_id):
                return "reconcile_mismatch"
        return None
