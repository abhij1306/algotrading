"""
Unified Fyers Client - Robust and Singleton
Handles authentication, token management, and data fetching.
Includes Circuit Breaker and Rate Limit handling.
"""
import os
import sys
import json
import logging
import time
import functools
from typing import Dict, Any, Optional, List
from fyers_apiv3 import fyersModel

# Setup Logging
logger = logging.getLogger(__name__)

# Constants
FYERS_TOKEN_PATH = os.path.join(os.getcwd(), "fyers", "config", "access_token.json")
MAX_RETRIES = 3
INITIAL_BACKOFF = 1  # Seconds

class CircuitBreakerOpen(Exception):
    pass

def retry_api_call(func):
    """Decorator to handle rate limits and transient errors"""
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        # Check Circuit Breaker
        if self.circuit_open:
            if time.time() - self.last_error_time > self.circuit_cooldown:
                logger.info("Circuit Breaker HALF-OPEN: Trying request...")
                self.circuit_open = False
                self.error_count = 0
            else:
                logger.warning("Circuit Breaker OPEN: Skipping request")
                return {"s": "error", "message": "Circuit Breaker Open"}

        retries = 0
        while retries <= MAX_RETRIES:
            try:
                result = func(self, *args, **kwargs)

                # Check for API-level errors in response (Fyers returns 's': 'error')
                if isinstance(result, dict) and result.get('s') == 'error':
                    msg = result.get('message', '').lower()
                    if 'limit' in msg or '429' in msg:
                        raise Exception(f"Rate Limit Hit: {msg}")

                # Success
                self.error_count = 0
                return result

            except Exception as e:
                msg = str(e).lower()
                is_rate_limit = 'limit' in msg or '429' in msg

                if is_rate_limit:
                    logger.warning(f"Rate Limit (429) hit in {func.__name__}: {e}")
                    # Trigger Circuit Breaker immediately on Rate Limit
                    self.circuit_open = True
                    self.last_error_time = time.time()
                    self.circuit_cooldown = 60 # Cool down for 60s
                    return {"s": "error", "message": "Rate Limit Hit - Circuit Open"}

                # Standard Retry Logic
                retries += 1
                if retries > MAX_RETRIES:
                    logger.error(f"Max retries reached for {func.__name__}: {e}")
                    self.error_count += 1
                    if self.error_count >= 5:
                         logger.critical("Too many consecutive errors. Opening Circuit Breaker.")
                         self.circuit_open = True
                         self.last_error_time = time.time()
                         self.circuit_cooldown = 30
                    return {"s": "error", "message": str(e)}

                sleep_time = INITIAL_BACKOFF * (2 ** (retries - 1))
                logger.info(f"Retrying {func.__name__} in {sleep_time}s... (Error: {e})")
                time.sleep(sleep_time)

    return wrapper

class FyersClient:
    """
    Singleton Fyers Client wrapper.
    Ensures valid session and handles API calls.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(FyersClient, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.fyers: Optional[fyersModel.FyersModel] = None
        self.client_id: Optional[str] = None
        self.access_token: Optional[str] = None

        # Circuit Breaker State
        self.circuit_open = False
        self.last_error_time = 0
        self.circuit_cooldown = 30 # seconds
        self.error_count = 0

        self._load_credentials()
        self._connect()
        self._initialized = True

    def _load_credentials(self):
        """Load credentials from file"""
        # Try multiple paths
        paths = [
            FYERS_TOKEN_PATH,
            os.path.join(os.getcwd(), "Fyers", "config", "access_token.json"),
            os.path.join(os.path.dirname(__file__), "..", "..", "..", "fyers", "config", "access_token.json")
        ]

        valid_path = None
        for p in paths:
            if os.path.exists(p):
                valid_path = p
                break

        if not valid_path:
            logger.error(f"Fyers token file not found in searched paths")
            return

        try:
            with open(valid_path, 'r') as f:
                data = json.load(f)
                self.client_id = data.get('client_id')
                self.access_token = data.get('access_token')
        except Exception as e:
            logger.error(f"Failed to load Fyers credentials: {e}")

    def _connect(self):
        """Initialize FyersModel instance"""
        if self.client_id and self.access_token:
            try:
                self.fyers = fyersModel.FyersModel(
                    client_id=self.client_id,
                    token=self.access_token,
                    log_path=""
                )
                # Verify session
                if not self.validate_token():
                    logger.warning("Fyers token appears invalid on connect")
            except Exception as e:
                logger.error(f"Error connecting to Fyers: {e}")

    def validate_token(self) -> bool:
        """Check if token is valid by making a lightweight call"""
        if not self.fyers:
            return False
        try:
            # get_profile is a lightweight call
            response = self.fyers.get_profile()
            if response.get('s') == 'ok':
                return True
            return False
        except Exception:
            return False

    @retry_api_call
    def get_quotes(self, symbols: List[str]) -> Dict[str, Any]:
        """
        Fetch quotes for a list of symbols
        symbols: List like ['NSE:RELIANCE-EQ', 'NSE:TCS-EQ']
        """
        if not self.fyers:
            return {}

        # Join symbols (max 50 per call ideally, but Fyers supports more)
        sym_str = ",".join(symbols)
        return self.fyers.quotes({"symbols": sym_str})

    @retry_api_call
    def get_historical_data(self, symbol: str, timeframe: str, range_from: str, range_to: str) -> Dict[str, Any]:
        """
        Fetch historical candle data
        timeframe: "1", "5", "D", etc.
        range_from/to: "YYYY-MM-DD"
        """
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

        return self.fyers.history(data)

    @retry_api_call
    def get_orderbook(self):
        if not self.fyers: return {}
        return self.fyers.orderbook()

    @retry_api_call
    def get_positions(self):
        if not self.fyers: return {}
        return self.fyers.positions()

    @retry_api_call
    def place_order(self, data: Dict[str, Any]):
        if not self.fyers: return {"s": "error", "message": "Client not connected"}
        return self.fyers.place_order(data)

# Global Accessor
fyers_client = FyersClient()

def get_fyers_client() -> FyersClient:
    return fyers_client
