"""The Fyers access-token file. HAT owns the store (`app.hat.execution.fyers_tokens`), so there is
one token file reader and writer; this legacy module re-exports it and adds the DEV_MODE switch
that only legacy code honours. K4 and K5 delete it with the legacy features."""

from app.hat.execution.fyers_tokens import (
    StoredToken,
    TokenStore,
    TokenStoreError,
    token_expiry,
    token_store,
)
from app.utils.env_loader import env_flag

__all__ = [
    "StoredToken",
    "TokenStore",
    "TokenStoreError",
    "fyers_client_disabled",
    "token_expiry",
    "token_store",
]


def fyers_client_disabled() -> bool:
    """DEV_MODE: the legacy FyersClient skips loading the token, so no Fyers call can be made.

    The one check shared by the legacy client and config.fyers_available(). HAT does not honour it.
    """
    return env_flag("DEV_MODE")
