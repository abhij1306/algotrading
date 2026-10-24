"""The Kite (Zerodha) access-token file: the only code that names it (HAT-OPS-051, as
`fyers_tokens` is for Fyers).

The file is the `fyers_tokens.TokenStore` format at its own path (`KITE_TOKEN_PATH`, gitignored).
The login flow (`kite_auth`) saves only what HAT reads back: the access token, the Kite user id
(`user_id`) and the API key as the client id; never the reply's refresh or public token.

A Kite access token is valid until 06:00 IST the next morning, whenever it was issued (Kite's
daily logout), so ``expires_at`` is the first 06:00 IST after the login.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from app.hat.time import IST
from app.paths import PROJECT_ROOT

from .fyers_tokens import TokenStore

__all__ = ["KITE_TOKEN_PATH", "KITE_USER_KEY", "kite_token_expiry", "kite_token_store"]

KITE_TOKEN_PATH = PROJECT_ROOT / "kite" / "config" / "access_token.json"
KITE_USER_KEY = "user_id"  # the token reply's field naming the Kite user (the account_ref)
KITE_EXPIRY_TIME = time(6, 0)


def kite_token_expiry(now: datetime) -> datetime:
    """The first 06:00 IST after ``now`` (timezone-aware): when a token issued at ``now``
    expires. The result is in IST."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(IST)
    expiry = datetime.combine(local.date(), KITE_EXPIRY_TIME, tzinfo=IST)
    return expiry if local < expiry else expiry + timedelta(days=1)


kite_token_store = TokenStore(
    KITE_TOKEN_PATH, expiry=kite_token_expiry, user_key=KITE_USER_KEY, label="Kite"
)
"""The process-wide store for the Kite token file."""
