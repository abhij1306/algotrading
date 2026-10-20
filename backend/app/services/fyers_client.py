"""
Unified Fyers Client - Robust and Singleton
Handles authentication, token management, and data fetching.
"""

import os
from datetime import UTC, datetime
from types import ModuleType
from typing import Any, Self

from ..utils.logger import get_logger
from .fyers.token_store import TokenStoreError, token_store
from .symbol_master import symbol_master

# Lazy import fyers_apiv3 to speed up startup
_fyers_model_module: ModuleType | None = None


def _get_fyers_model() -> ModuleType:
    global _fyers_model_module
    if _fyers_model_module is None:
        from fyers_apiv3 import fyersModel

        _fyers_model_module = fyersModel
    return _fyers_model_module


# Setup Logging
logger = get_logger("services.fyers_client")


class FyersClient:
    """
    Singleton Fyers Client wrapper.
    Ensures valid session and handles API calls.
    """

    _instance = None
    _fyers: Any = None

    @property
    def fyers(self) -> Any:  # noqa: ANN401 - the untyped fyers_apiv3 FyersModel
        """The SDK model, or None once the loaded token has passed its expiry.

        Every caller that reaches the SDK goes through here, so a token that expires while
        the app runs (the file, and so its mtime, unchanged) stops being used at once,
        even by calls that skip validate_token().
        """
        if self._fyers is not None and self.token_expires_at is not None:
            if datetime.now(UTC) > self.token_expires_at:
                logger.warning("Fyers token expired on %s; dropping it", self.token_expires_at)
                self._drop_credentials()
        return self._fyers

    @fyers.setter
    def fyers(self, model: Any) -> None:  # noqa: ANN401 - the untyped fyers_apiv3 FyersModel
        self._fyers = model

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return

        self.fyers = None
        self.client_id = None
        self.access_token = None
        self.token_expires_at = None
        self._token_file_mtime_ns = None
        self._expired_token_mtime_ns: int | None = None
        self._connecting = False
        self._validating_token = False
        self._missing_token_file_logged = False
        self._missing_credentials_logged = False

        # Skip initialization if DEV_MODE is set
        if os.getenv("DEV_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}:
            logger.info("[DEV_MODE] Skipping Fyers client initialization")
            self._initialized = True
            return

        self._load_credentials()
        # Connect lazily without immediate token validation.
        # Validation happens in startup validation and runtime checks.
        self._connect(skip_validation=True)
        self._initialized = True

    def _load_credentials(self) -> None:
        """Load credentials from the token file, with expiry checking.

        Fails closed: when the file is gone, unusable or unreadable, the cached credentials
        and SDK model are dropped so no call goes out with an old token.
        """
        store = token_store
        try:
            mtime_ns = store.mtime_ns()
            if mtime_ns is None:
                if not self._missing_token_file_logged:
                    logger.warning("Fyers token file not found at %s", store.path)
                    self._missing_token_file_logged = True
                self._drop_credentials()
                return
            self._token_file_mtime_ns = mtime_ns
            token = store.load()
        except TokenStoreError:
            logger.exception("Failed to load Fyers credentials")
            self._drop_credentials()
            return
        if token is None:
            self._drop_credentials()  # the store logged why the file is not a usable token
            return

        if not token.is_usable():
            # Fail closed: an expired token (or one without a client id) is never installed,
            # so calls that skip validate_token() cannot use it either. Warn once per file.
            if self._expired_token_mtime_ns != mtime_ns:
                if token.client_id:
                    logger.warning("Fyers token expired on %s", token.expires_at)
                else:
                    logger.warning("Fyers token file has no client id; ignoring it")
                self._expired_token_mtime_ns = mtime_ns
            self._drop_credentials()
            return

        self.client_id = token.client_id
        self.access_token = token.access_token
        self.token_expires_at = token.expires_at

        # Credentials are available again;
        # allow a future missing-credentials warning if needed.
        if self.client_id and self.access_token:
            self._missing_credentials_logged = False

    def _drop_credentials(self) -> None:
        """Forget the token and the SDK model built from it (logout or unusable file)."""
        self.client_id = None
        self.access_token = None
        self.token_expires_at = None
        self._token_file_mtime_ns = None
        self.fyers = None

    def _refresh_credentials_if_changed(self) -> bool:
        """
        Reload token credentials when token file changes on disk.
        Returns True if credentials were reloaded. A removed token file drops the
        cached credentials (logout) and returns False.
        """
        try:
            current_mtime_ns = token_store.mtime_ns()
            if current_mtime_ns is None:
                if self.access_token or self.fyers is not None:
                    logger.info("Fyers token file removed; dropping cached credentials")
                    self._drop_credentials()
                return False
            if (
                self._token_file_mtime_ns == current_mtime_ns
                and self.client_id
                and self.access_token
            ):
                return False

            old_token = self.access_token
            self._load_credentials()
            return bool(self.access_token and self.access_token != old_token)
        except TokenStoreError:
            logger.exception("Failed checking token file changes")
            self._drop_credentials()
            return False
        except Exception as e:
            logger.exception(f"Failed checking token file changes: {e}")
            return False

    def _connect(
        self, *, skip_validation: bool = False, suppress_missing_credentials_log: bool = False
    ) -> None:
        """Initialize FyersModel instance"""
        if self._connecting:
            logger.debug("Skipping nested Fyers _connect call")
            return

        if not self.client_id or not self.access_token:
            if not suppress_missing_credentials_log and not self._missing_credentials_logged:
                logger.warning("Fyers credentials not loaded.")
                self._missing_credentials_logged = True
            return

        self._connecting = True
        try:
            self.fyers = _get_fyers_model().FyersModel(
                client_id=self.client_id, token=self.access_token, log_path=""
            )

            if skip_validation:
                logger.info("[OK] Fyers client connected (validation skipped)")
            elif self.validate_token():
                logger.info("[OK] Fyers client connected successfully")
            else:
                logger.warning("[WARN] Fyers token validation failed.")

        except Exception as e:
            logger.exception(f"Error connecting to Fyers: {e}")
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
                self._connect(skip_validation=True, suppress_missing_credentials_log=True)
                if not self.fyers:
                    return False

            # Avoid noisy API validation calls when token is already known expired.
            if self.is_token_expired():
                return False
            return self._validate_with_profile()
        finally:
            self._validating_token = False

    def _profile_ok(self) -> bool:
        response = self.fyers.get_profile()
        if response.get("s") == "ok":
            return True
        logger.warning("[WARN] Fyers token invalid: %s", response)
        return False

    def _validate_with_profile(self) -> bool:
        """Profile call, retried once after reloading a token that changed on disk.

        The retry handles a fresh login while the backend is running.
        """
        try:
            if self._profile_ok():
                return True
            if self._refresh_credentials_if_changed():
                self._connect(skip_validation=True)
                return self._profile_ok()
            return False
        except Exception as e:
            logger.exception(f"[ERROR] Token validation failed: {e}")
            if not self._refresh_credentials_if_changed():
                return False
            try:
                self._connect(skip_validation=True)
                return self._profile_ok()
            except Exception as retry_error:
                logger.exception(f"[ERROR] Token validation retry failed: {retry_error}")
                return False

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
            logger.exception(f"Error fetching quotes: {e}")
            return {}

    def get_parsed_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Fetch and parse quotes into a standardized format"""
        response = self.get_quotes(symbols)
        if response.get("s") != "ok" or "d" not in response:
            return {}

        quotes_dict = {}
        for quote in response["d"]:
            symbol_raw = quote.get("n")
            if not symbol_raw:
                continue

            symbol = symbol_master.to_db(symbol_raw)
            v = quote.get("v", {})

            ltp = v.get("lp", 0)
            prev_close = v.get("prev_close_price", ltp)

            if prev_close and prev_close > 0:
                change_pct = ((ltp - prev_close) / prev_close) * 100
            else:
                change_pct = 0

            quotes_dict[symbol] = {
                "ltp": ltp,
                "volume": v.get("volume", 0),
                "high": v.get("high_price", 0),
                "low": v.get("low_price", 0),
                "open": v.get("open_price", 0),
                "prev_close": prev_close,
                "change_pct": round(change_pct, 2),
            }

        return quotes_dict

    def get_historical_data(
        self, symbol: str, timeframe: str, range_from: str, range_to: str
    ) -> dict[str, Any]:
        """Fetch historical candle data"""
        if not self.fyers:
            return {}

        data = {
            "symbol": symbol,
            "resolution": timeframe,
            "date_format": "1",
            "range_from": range_from,
            "range_to": range_to,
            "cont_flag": "1",
        }

        try:
            response = self.fyers.history(data)
            return response
        except Exception as e:
            logger.exception(f"Error fetching history for {symbol}: {e}")
            return {}

    def get_orderbook(self) -> dict[str, Any]:
        if not self.fyers:
            return {}
        return self.fyers.orderbook()

    def get_positions(self) -> dict[str, Any]:
        if not self.fyers:
            return {}
        return self.fyers.positions()

    def place_order(self, data: dict[str, Any]) -> dict[str, Any]:
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
    FyersClient._instance = None
