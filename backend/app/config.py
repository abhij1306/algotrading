"""
Configuration module for loading environment variables and settings
"""

import logging
import os

from .services.fyers.token_store import TokenStoreError, fyers_client_disabled, token_store
from .utils.env_loader import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def fyers_available() -> bool:
    """Whether Fyers API calls can be made right now, read at call time.

    True only for an unexpired saved login token with a client id: the Fyers client reads its
    credentials from the same TokenStore, so this never reports a source the client can't use.
    False under DEV_MODE, where FyersClient skips loading the token. Never raises: an
    unreadable token file is "no".
    """
    if fyers_client_disabled():
        return False
    try:
        token = token_store.load()
    except TokenStoreError:
        logger.warning("Failed to read the Fyers token file %s", token_store.path)
        return False
    return token is not None and token.is_usable()


# The frontend origins (the Vite dev server ports) allowed by CORS and by the Origin check on
# WS /api/websocket/stream. Override with CORS_ORIGINS, a comma-separated list.
DEFAULT_ALLOWED_ORIGINS = (
    "http://localhost:3000,http://localhost:3001,http://127.0.0.1:3000,http://127.0.0.1:3001"
)


def parse_origins(raw: str) -> tuple[str, ...]:
    """Split a comma-separated origin list, dropping blanks and stray whitespace."""
    return tuple(origin.strip() for origin in raw.split(",") if origin.strip())


class Config:
    # Server settings
    PORT = int(os.getenv("PORT", 8000))
    CACHE_TTL = int(os.getenv("CACHE_TTL", 3600))
    ENV = os.getenv("ENV", "development")
    ALLOWED_ORIGINS = parse_origins(os.getenv("CORS_ORIGINS", DEFAULT_ALLOWED_ORIGINS))

    # Data paths - now using index_universe_loader service
    # NSE_FNO_UNIVERSE_PATH is deprecated - use index_universe_loader.get_index_symbols("NIFTY500")

    # Screening parameters
    MAX_TICKERS = 50  # Cap for combined list
    MIN_INTRADAY_SCORE = 0  # Show all stocks
    MIN_SWING_SCORE = 0  # Show all stocks

    @staticmethod
    def get_mode() -> str:
        """Return data fetching mode"""
        return "Fyers" if fyers_available() else "Database only"


config = Config()
