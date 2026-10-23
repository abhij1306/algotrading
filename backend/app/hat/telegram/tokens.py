"""Signed, single-use approval tokens (08 §4; HAT-TG-040, HAT-TG-041, HAT-TG-044, HAT-SCH-056).

A confirm-mode card carries two buttons. Each button's callback data is

    action (1 char) + base64url(signal id 16 bytes | expiry 8 bytes | MAC 16 bytes)

where the MAC is HMAC-SHA256 under `HAT_APPROVAL_SECRET` over a domain tag, the action, the
signal id and the expiry (whole epoch seconds), cut to 128 bits. The 55 characters fit
Telegram's 64-byte callback limit.

The card itself has one token, `card_token` (the same MAC over the signal id and expiry with
the card tag). It is issued when the approval is written (`trades.send_approval` calls
`ApprovalSigner.issue`) and only its sha256 is stored, as `hat_approval.token_hash`. A button is
accepted only when its MAC verifies, the approval of its signal exists, its expiry is that
approval's `expires_at`, and the stored hash equals the hash of the card token recomputed under
the secret: so a button is bound to the card the service issued, under this secret. Single use
is the approval's one decision (`approvals.take` and `skip` refuse a decided card under the
bot's lock), which also spends the card's other button.

Nothing here touches the database or logs; the raw tokens exist only in the buttons.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from ..errors import HatConfigError
from .config import MIN_SECRET_BYTES

__all__ = [
    "ACTIONS",
    "CONFIRM",
    "SKIP",
    "TAKE",
    "ApprovalSigner",
    "ButtonClaim",
    "expiry_seconds",
    "token_hash",
]

TAKE = "t"
SKIP = "s"
CONFIRM = "c"  # the second-confirmation button (confirm.py); not an approval action
ACTIONS = (TAKE, SKIP)
_MAC_BYTES = 16
_ID_BYTES = 16
_EXPIRY_BYTES = 8
_BODY_BYTES = _ID_BYTES + _EXPIRY_BYTES + _MAC_BYTES
_CARD_TAG = b"hat-approval-card|"
_BUTTON_TAG = b"hat-approval-button|"


@dataclass(frozen=True)
class ButtonClaim:
    """What a button's callback data claims once its MAC verifies."""

    action: str
    signal_id: uuid.UUID
    expires: int


def expiry_seconds(expires_at: datetime) -> int:
    """The expiry a token binds: whole epoch seconds (a naive value is read as UTC)."""
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return int(expires_at.timestamp())


def token_hash(token: str) -> str:
    """The stored form of a card token (`hat_approval.token_hash`)."""
    return hashlib.sha256(token.encode()).hexdigest()


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode(text: str) -> bytes | None:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError:  # binascii.Error is a ValueError
        return None


class ApprovalSigner:
    """Issues and verifies approval tokens under one secret."""

    def __init__(self, secret: bytes) -> None:
        if len(secret) < MIN_SECRET_BYTES:
            raise HatConfigError(f"the approval secret is shorter than {MIN_SECRET_BYTES} bytes")
        self._secret = secret

    def __repr__(self) -> str:
        return "ApprovalSigner(<secret>)"

    def _mac(self, tag: bytes, signal_id: uuid.UUID, expires: int) -> bytes:
        message = tag + signal_id.bytes + expires.to_bytes(_EXPIRY_BYTES, "big", signed=True)
        return hmac.new(self._secret, message, hashlib.sha256).digest()[:_MAC_BYTES]

    def _body(self, tag: bytes, signal_id: uuid.UUID, expires: int) -> str:
        stamp = expires.to_bytes(_EXPIRY_BYTES, "big", signed=True)
        return _encode(signal_id.bytes + stamp + self._mac(tag, signal_id, expires))

    def issue(self, signal_id: uuid.UUID, expires_at: datetime) -> str:
        """The card token of an approval: its sha256 is what `hat_approval.token_hash` holds."""
        return self._body(_CARD_TAG, signal_id, expiry_seconds(expires_at))

    def card_hash(self, signal_id: uuid.UUID, expires_at: datetime) -> str:
        return token_hash(self.issue(signal_id, expires_at))

    def button(self, action: str, signal_id: uuid.UUID, expires_at: datetime) -> str:
        """The callback data of a card's Take or Skip button."""
        if action not in ACTIONS:
            raise ValueError("unknown approval action")
        tag = _BUTTON_TAG + action.encode() + b"|"
        return action + self._body(tag, signal_id, expiry_seconds(expires_at))

    def verify(self, data: str) -> ButtonClaim | None:
        """The claim of a button whose MAC verifies under the secret, else None. Binding to a
        stored approval is the caller's (`matches`)."""
        if not data or data[0] not in ACTIONS:
            return None
        raw = _decode(data[1:])
        if raw is None or len(raw) != _BODY_BYTES:
            return None
        signal_id = uuid.UUID(bytes=raw[:_ID_BYTES])
        expires = int.from_bytes(raw[_ID_BYTES : _ID_BYTES + _EXPIRY_BYTES], "big", signed=True)
        tag = _BUTTON_TAG + data[0].encode() + b"|"
        if not hmac.compare_digest(
            raw[_ID_BYTES + _EXPIRY_BYTES :], self._mac(tag, signal_id, expires)
        ):
            return None
        return ButtonClaim(data[0], signal_id, expires)

    def matches(self, claim: ButtonClaim, expires_at: datetime, stored_hash: str) -> bool:
        """The claim names this approval's expiry and the approval's stored hash is the one of
        the card token this signer issues for it."""
        if claim.expires != expiry_seconds(expires_at):
            return False
        expected = self.card_hash(claim.signal_id, expires_at)
        return hmac.compare_digest(expected.encode(), (stored_hash or "").encode())
