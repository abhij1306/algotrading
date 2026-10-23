"""Second confirmations for `/mode … auto`, `/arm` and `/flatten` (HAT-TG-033,
HAT-GOV-082; the model is `verification/tla/ApprovalToken.tla`, SecondConfirmRequired).

The command does nothing but ask: the reply carries one Confirm button whose callback data is a
random 128-bit nonce. The action runs only when that button is pressed in the same allowlisted
chat before the confirmation expires (`CONFIRM_WINDOW`, the 5 minutes of an approval card), and
a confirmation is consumed at most once: `take` removes it under a lock, so a double tap or a
replayed callback finds nothing. Pending confirmations live in memory only: a restart forgets
them, which can only cancel an action, never perform one.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..time import as_utc
from .tokens import CONFIRM

__all__ = ["CONFIRM_WINDOW", "Confirmation", "PendingConfirmations"]

CONFIRM_WINDOW = timedelta(minutes=5)
_MAX_PENDING = 16


@dataclass(frozen=True)
class Confirmation:
    """An action waiting for its second confirmation: `action` is flatten, arm or mode_auto,
    `bots` the bot ids a mode change names."""

    action: str
    chat_id: int
    expires_at: datetime
    bots: tuple[str, ...] = field(default=())


class PendingConfirmations:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, Confirmation] = {}

    def ask(
        self, action: str, chat_id: int, now: datetime, bots: tuple[str, ...] = ()
    ) -> tuple[str, Confirmation]:
        """Register a confirmation and return its button's callback data."""
        nonce = secrets.token_urlsafe(16)
        pending = Confirmation(action, chat_id, as_utc(now) + CONFIRM_WINDOW, bots)
        with self._lock:
            self._drop_expired(now)
            while len(self._pending) >= _MAX_PENDING:
                self._pending.pop(next(iter(self._pending)))
            self._pending[nonce] = pending
        return CONFIRM + nonce, pending

    def action_of(self, data: str) -> str | None:
        """The action a confirmation button names while it is pending, without consuming it."""
        nonce = data[len(CONFIRM) :] if data.startswith(CONFIRM) else ""
        with self._lock:
            pending = self._pending.get(nonce)
        return None if pending is None else pending.action

    def take(self, data: str, chat_id: int, now: datetime) -> tuple[Confirmation | None, str]:
        """Consume the confirmation a button names: (it, "ok") when it is pending, from the
        same chat and not expired; else (None, the reason). Consumed even when refused."""
        nonce = data[len(CONFIRM) :] if data.startswith(CONFIRM) else ""
        with self._lock:
            pending = self._pending.pop(nonce, None)
        if pending is None:
            return None, "no_pending_confirmation"
        if pending.chat_id != chat_id:
            return None, "wrong_chat"
        if as_utc(now) >= pending.expires_at:
            return None, "confirmation_expired"
        return pending, "ok"

    def _drop_expired(self, now: datetime) -> None:
        moment = as_utc(now)
        for nonce in [n for n, c in self._pending.items() if moment >= c.expires_at]:
            del self._pending[nonce]
