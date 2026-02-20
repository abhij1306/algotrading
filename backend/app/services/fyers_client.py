"""
Unified Fyers Client - Robust and Singleton
Handles authentication, token management, and data fetching.
"""
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..utils.logger import get_logger
from .symbol_master import symbol_master

# Lazy import fyers_apiv3 to speed up startup
fyersModel = None
def _get_fyers_model():
    global fyersModel
    if fyersModel is None:
        from fyers_apiv3 import fyersModel as _fm
        fyersModel = _fm
    return fyersModel

# Setup Logging
logger = get_logger("services.fyers_client")

# FIXED: Use Path and project root instead of os.getcwd()
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
FYERS_TOKEN_PATH = PROJECT_ROOT / "fyers" / "config" / "access_token.json"

class FyersClient:
    """
    Singleton Fyers Client wrapper.
    Ensures valid session and handles API calls.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.fyers = None
        self.client_id = None
        self.access_token = None
        self.token_expires_at = None
        self._token_file_mtime_ns = None
        self._connecting = False
        self._validating_token = False

        # Skip initialization if DEV_MODE is set
        if os.getenv("DEV_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}:
            logger.info("[DEV_MODE] Skipping Fyers client initialization")
            self._initialized = True
            return

        self._load_credentials()
        self._connect()
        self._initialized = True

    def _load_credentials(self):
        """Load credentials from file with expiry checking"""
        if not FYERS_TOKEN_PATH.exists():
            logger.warning(f"Fyers token file not found at {FYERS_TOKEN_PATH}")
            return

        try:
            self._token_file_mtime_ns = FYERS_TOKEN_PATH.stat().st_mtime_ns
            with open(FYERS_TOKEN_PATH) as f:
                data = json.load(f)
                self.client_id = data.get('client_id')
                self.access_token = data.get('access_token')

                expires_str = data.get('expires_at')
                if expires_str:
                    try:
                        self.token_expires_at = datetime.fromisoformat(expires_str)
                        if self.token_expires_at.tzinfo is None:
                            self.token_expires_at = self.token_expires_at.replace(tzinfo=UTC)

                        now_utc = datetime.now(UTC)
                        if now_utc > self.token_expires_at:
                            logger.warning(f"Fyers token expired on {self.token_expires_at}")
                    except ValueError:
                        pass

        except Exception as e:
            logger.error(f"Failed to load Fyers credentials: {e}")

    def _refresh_credentials_if_changed(self) -> bool:
        """
        Reload token credentials when token file changes on disk.
        Returns True if credentials were reloaded.
        """
        try:
            if not FYERS_TOKEN_PATH.exists():
                return False
            current_mtime_ns = FYERS_TOKEN_PATH.stat().st_mtime_ns
            if self._token_file_mtime_ns == current_mtime_ns and self.client_id and self.access_token:
                return False

            old_token = self.access_token
            self._load_credentials()
            return bool(self.access_token and self.access_token != old_token)
        except Exception as e:
            logger.error(f"Failed checking token file changes: {e}")
            return False

    def _connect(self, *, skip_validation: bool = False):
        """Initialize FyersModel instance"""
        if self._connecting:
            logger.debug("Skipping nested Fyers _connect call")
            return

        if not self.client_id or not self.access_token:
            logger.warning("Fyers credentials not loaded.")
            return

        self._connecting = True
        try:
            self.fyers = _get_fyers_model().FyersModel(
                client_id=self.client_id,
                token=self.access_token,
                log_path=""
            )

            if skip_validation:
                logger.info("[OK] Fyers client connected (validation skipped)")
            elif self.validate_token():
                logger.info("[OK] Fyers client connected successfully")
            else:
                logger.warning("[WARN] Fyers token validation failed.")

        except Exception as e:
            logger.error(f"Error connecting to Fyers: {e}")
        finally:
            self._connecting = False

    def validate_token(self) -> bool:
        """Check if token is valid by making a lightweight call"""
        if self._validating_token:
            logger.debug("Skipping nested token validation call")
            return bool(self.fyers)

        self._validating_token = True
        try:
            token_updated = self._refresh_credentials_if_changed()
            if token_updated:
                logger.info("Detected updated Fyers token on disk, reconnecting client.")
                self._connect(skip_validation=True)

            if not self.fyers:
                # One extra attempt in case client wasn't initialized during startup.
                self._connect(skip_validation=True)
                if not self.fyers:
                    return False

            def _profile_ok() -> bool:
                response = self.fyers.get_profile()
                if response.get('s') == 'ok':
                    return True
                logger.warning(f"[ERROR] Fyers token invalid: {response}")
                return False

            try:
                if _profile_ok():
                    return True

                # Retry once after reloading from disk to handle fresh-login while backend is running.
                if self._refresh_credentials_if_changed():
                    self._connect(skip_validation=True)
                    return _profile_ok()
                return False
            except Exception as e:
                logger.error(f"[ERROR] Token validation failed: {e}")
                if self._refresh_credentials_if_changed():
                    try:
                        self._connect(skip_validation=True)
                        return _profile_ok()
                    except Exception as retry_error:
                        logger.error(f"[ERROR] Token validation retry failed: {retry_error}")
                return False
        finally:
            self._validating_token = False

    def is_token_expired(self) -> bool:
        """Check if token has expired based on expiry date"""
        if not self.token_expires_at:
            return False
        now_utc = datetime.now(UTC)
        return now_utc > self.token_expires_at

    def get_quotes(self, symbols: list[str]) -> dict[str, Any]:
        """Fetch raw quotes for a list of symbols"""
        if not self.fyers:
            return {}

        try:
            sym_str = ",".join(symbols)
            response = self.fyers.quotes({"symbols": sym_str})
            return response
        except Exception as e:
            logger.error(f"Error fetching quotes: {e}")
            return {}

    def get_parsed_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Fetch and parse quotes into a standardized format"""
        response = self.get_quotes(symbols)
        if response.get('s') != 'ok' or 'd' not in response:
            return {}

        quotes_dict = {}
        for quote in response['d']:
            symbol_raw = quote.get('n')
            if not symbol_raw:
                continue

            symbol = symbol_master.to_db(symbol_raw)
            v = quote.get('v', {})

            ltp = v.get('lp', 0)
            prev_close = v.get('prev_close_price', ltp)

            if prev_close and prev_close > 0:
                change_pct = ((ltp - prev_close) / prev_close) * 100
            else:
                change_pct = 0

            quotes_dict[symbol] = {
                'ltp': ltp,
                'volume': v.get('volume', 0),
                'high': v.get('high_price', 0),
                'low': v.get('low_price', 0),
                'open': v.get('open_price', 0),
                'prev_close': prev_close,
                'change_pct': round(change_pct, 2),
            }

        return quotes_dict

    def get_historical_data(self, symbol: str, timeframe: str, range_from: str, range_to: str) -> dict[str, Any]:
        """Fetch historical candle data"""
        if not self.fyers:
            return {}

        data = {
            "symbol": symbol,
            "resolution": timeframe,
            "date_format": "1",
            "range_from": range_from,
            "range_to": range_to,
            "cont_flag": "1"
        }

        try:
            response = self.fyers.history(data)
            return response
        except Exception as e:
            logger.error(f"Error fetching history for {symbol}: {e}")
            return {}

    def get_orderbook(self):
        if not self.fyers:
            return {}
        return self.fyers.orderbook()

    def get_positions(self):
        if not self.fyers:
            return {}
        return self.fyers.positions()

    def place_order(self, data: dict[str, Any]):
        if not self.fyers:
            return {"s": "error", "message": "Client not connected"}
        return self.fyers.place_order(data)

# Global Accessor - LAZY INITIALIZATION
_fyers_client = None

def get_fyers_client() -> FyersClient:
    global _fyers_client
    if _fyers_client is None:
        _fyers_client = FyersClient()
    return _fyers_client

def reset_fyers_client() -> None:
    """Reset singleton so fresh credentials are loaded on next access."""
    global _fyers_client
    _fyers_client = None
