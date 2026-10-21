"""What the order path reads before it places anything (HAT-BRK-110, -111)."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

__all__ = ["ExecutionGuard"]


@dataclass
class ExecutionGuard:
    """What the order path reads: has this session reconciled, and who is blocked."""

    reconciled: bool = False
    blocked_bots: set[uuid.UUID] = field(default_factory=set)
    blocked_accounts: set[uuid.UUID] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def begin_session(self) -> None:
        """A (re)start or a new trading session: nothing is placed until reconcile succeeds."""
        with self._lock:
            self.reconciled = False

    def update(
        self,
        *,
        reconciled: bool,
        blocked_bots: set[uuid.UUID],
        blocked_accounts: set[uuid.UUID],
    ) -> None:
        with self._lock:
            self.reconciled = reconciled
            self.blocked_bots = set(blocked_bots)
            self.blocked_accounts = set(blocked_accounts)

    def entry_blocked(self, bot_id: uuid.UUID, account_id: uuid.UUID) -> bool:
        return bot_id in self.blocked_bots or account_id in self.blocked_accounts

    def refusal(self, bot_id: uuid.UUID, account_id: uuid.UUID, *, is_entry: bool) -> str | None:
        """Why an order may not go to the broker now (a fixed code), or None. Nothing is placed
        before a reconciliation; entries also stop while the bot or account is blocked."""
        with self._lock:
            if not self.reconciled:
                return "not_reconciled"
            if is_entry and self.entry_blocked(bot_id, account_id):
                return "reconcile_mismatch"
        return None
