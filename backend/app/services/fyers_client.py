"""
Unified Fyers Client - Robust and Singleton
Handles authentication, token management, and data fetching.

FIXED: Token path resolution, expiry checking, validation
"""
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, List
from fyers_apiv3 import fyersModel
from ..utils.logger import get_logger

# Setup Logging
logger = get_logger("services.fyers_client")

# FIXED: Use Path and project root instead of os.getcwd()
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
FYERS_TOKEN_PATH = PROJECT_ROOT / "fyers" / "config" / "access_token.json"

class FyersClient:
    """
    Singleton Fyers Client wrapper.
    Ensures valid session and handles API calls.
    
    FIXED: Proper token validation, expiry checking, and path handling
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
        self.token_expires_at: Optional[datetime] = None
        
        self._load_credentials()
        self._connect()
        self._initialized = True

    def _load_credentials(self):
        """Load credentials from file with expiry checking"""
        if not FYERS_TOKEN_PATH.exists():
            logger.error(f"Fyers token file not found at {FYERS_TOKEN_PATH}")
            logger.info(f"Expected location: {FYERS_TOKEN_PATH.absolute()}")
            logger.info("Run 'python fyers/fyers_login.py' to generate token")
            return

        try:
            with open(FYERS_TOKEN_PATH, 'r') as f:
                data = json.load(f)
                self.client_id = data.get('client_id')
                self.access_token = data.get('access_token')
                
                # Check expiry if provided
                expires_str = data.get('expires_at')
                if expires_str:
                    try:
                        self.token_expires_at = datetime.fromisoformat(expires_str)
                        
                        # Warn if token expired
                        if datetime.now() > self.token_expires_at:
                            logger.warning(f"Fyers token expired on {self.token_expires_at}")
                            logger.warning("Please run 'python fyers/fyers_login.py' to refresh")
                    except ValueError:
                        logger.debug(f"Could not parse expiry date: {expires_str}")
                        
        except Exception as e:
            logger.error(f"Failed to load Fyers credentials: {e}")

    def _connect(self):
        """Initialize FyersModel instance"""
        if not self.client_id or not self.access_token:
            logger.warning("Fyers credentials not loaded. Client will not be available.")
            return
            
        try:
            self.fyers = fyersModel.FyersModel(
                client_id=self.client_id,
                token=self.access_token,
                log_path=""
            )
            
            # Verify session
            if self.validate_token():
                logger.info("✅ Fyers client connected successfully")
            else:
                logger.warning("⚠️ Fyers token validation failed. Please re-login.")
                
        except Exception as e:
            logger.error(f"Error connecting to Fyers: {e}")

    def validate_token(self) -> bool:
        """Check if token is valid by making a lightweight call"""
        if not self.fyers:
            logger.debug("Fyers client not initialized")
            return False
            
        try:
            # get_profile is a lightweight call
            response = self.fyers.get_profile()
            if response.get('s') == 'ok':
                logger.debug("✅ Fyers token is valid")
                return True
            else:
                logger.warning(f"❌ Fyers token invalid: {response}")
                return False
        except Exception as e:
            logger.error(f"❌ Token validation failed: {e}")
            return False
    
    def is_token_expired(self) -> bool:
        """Check if token has expired based on expiry date"""
        if not self.token_expires_at:
            return False  # Unknown expiry, assume valid
        return datetime.now() > self.token_expires_at

    def get_quotes(self, symbols: List[str]) -> Dict[str, Any]:
        """
        Fetch raw quotes for a list of symbols
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

    def get_parsed_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """
        Fetch and parse quotes into a standardized format
        symbols: List like ['NSE:RELIANCE-EQ', 'NSE:TCS-EQ']
        Returns mapping of symbol name (unprefixed) to quote data
        """
        response = self.get_quotes(symbols)
        if response.get('s') != 'ok' or 'd' not in response:
            return {}

        quotes_dict = {}
        for quote in response['d']:
            # Handle both formats: NSE:RELIANCE-EQ or NSE:NIFTY50-INDEX
            # Use .get() to avoid KeyError if 'n' is missing
            symbol_raw = quote.get('n')
            if not symbol_raw:
                # Skip quotes without a valid symbol
                continue
            
            # Strip both NSE: and BSE: prefixes, and -EQ or -INDEX suffixes
            symbol = symbol_raw.replace('NSE:', '').replace('BSE:', '').replace('-EQ', '').replace('-INDEX', '')
            v = quote.get('v', {})

            ltp = v.get('lp', 0)
            prev_close = v.get('prev_close_price', ltp)

            # Calculate percentage change
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
