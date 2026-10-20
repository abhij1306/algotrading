"""
Configuration module for loading environment variables and settings
"""

import logging
import os

from .services.fyers.token_store import TokenStoreError, token_store
from .utils.env_loader import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def fyers_available() -> bool:
    """Whether Fyers API calls can be made right now, read at call time.

    True only for an unexpired saved login token with a client id: the Fyers client reads its
    credentials from the same TokenStore, so this never reports a source the client can't use.
    Never raises: an unreadable token file is "no".
    """
    try:
        token = token_store.load()
    except TokenStoreError:
        logger.warning("Failed to read the Fyers token file %s", token_store.path)
        return False
    return token is not None and token.is_usable()


class Config:
    # Server settings
    PORT = int(os.getenv("PORT", 8000))
    CACHE_TTL = int(os.getenv("CACHE_TTL", 3600))
    ENV = os.getenv("ENV", "development")

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
