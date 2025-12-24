"""
Unified Fyers Client - Robust and Singleton
Handles authentication, token management, and data fetching.
"""
import os
import sys
import json
import logging
import datetime
from typing import Dict, Any, Optional, List
from fyers_apiv3 import fyersModel

# Setup Logging
logger = logging.getLogger(__name__)

# Constants
FYERS_TOKEN_PATH = os.path.join(os.getcwd(), "fyers", "config", "access_token.json")

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
        self._load_credentials()
        self._connect()
        self._initialized = True

    def _load_credentials(self):
        """Load credentials from file"""
        if not os.path.exists(FYERS_TOKEN_PATH):
            logger.error(f"Fyers token file not found at {FYERS_TOKEN_PATH}")
            return

        try:
            with open(FYERS_TOKEN_PATH, 'r') as f:
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

    def get_quotes(self, symbols: List[str]) -> Dict[str, Any]:
        """
        Fetch quotes for a list of symbols
        symbols: List like ['NSE:RELIANCE-EQ', 'NSE:TCS-EQ']
        """
        if not self.fyers:
            return {}

        try:
            # Join symbols (max 50 per call ideally, but Fyers supports more)
            sym_str = ",".join(symbols)
            response = self.fyers.quotes({"symbols": sym_str})
            return response
        except Exception as e:
            logger.error(f"Error fetching quotes: {e}")
            return {}

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

        try:
            response = self.fyers.history(data)
            return response
        except Exception as e:
            logger.error(f"Error fetching history for {symbol}: {e}")
            return {}

    def get_orderbook(self):
        if not self.fyers: return {}
        return self.fyers.orderbook()

    def get_positions(self):
        if not self.fyers: return {}
        return self.fyers.positions()

    def place_order(self, data: Dict[str, Any]):
        if not self.fyers: return {"s": "error", "message": "Client not connected"}
        return self.fyers.place_order(data)

# Global Accessor
fyers_client = FyersClient()

def get_fyers_client() -> FyersClient:
    return fyers_client
