"""
Configuration module for loading environment variables and settings
"""
import os
import json
from pathlib import Path
# from dotenv import load_dotenv # Removed to fix PyInstaller build
from .utils.env_loader import load_dotenv

load_dotenv()

class Config:
    # Try to load Fyers credentials from fyers/config/access_token.json
    FYERS_TOKEN_FILE = Path(__file__).parent.parent.parent / 'fyers' / 'config' / 'access_token.json'
    
    FYERS_CLIENT_ID = ''
    FYERS_SECRET_KEY = ''
    FYERS_ACCESS_TOKEN = ''
    
    # Load from token file if it exists
    if FYERS_TOKEN_FILE.exists():
        try:
            with open(FYERS_TOKEN_FILE, 'r') as f:
                token_data = json.load(f)
                FYERS_CLIENT_ID = token_data.get('client_id', '')
                FYERS_ACCESS_TOKEN = token_data.get('access_token', '')
                # Secret key not needed for API calls, only for login
                FYERS_SECRET_KEY = 'loaded_from_token_file'
        except Exception as e:
            print(f"Warning: Could not load Fyers token file: {e}")
    
    # Fallback to environment variables
    if not FYERS_CLIENT_ID:
        FYERS_CLIENT_ID = os.getenv('FYERS_CLIENT_ID', '')
    if not FYERS_SECRET_KEY:
        FYERS_SECRET_KEY = os.getenv('FYERS_SECRET_KEY', '')
    if not FYERS_ACCESS_TOKEN:
        FYERS_ACCESS_TOKEN = os.getenv('FYERS_ACCESS_TOKEN', '')
    
    # Server settings
    PORT = int(os.getenv('PORT', 8000))
    CACHE_TTL = int(os.getenv('CACHE_TTL', 3600))
    ENV = os.getenv('ENV', 'development')
    
    # Fyers API availability
    HAS_FYERS = bool(FYERS_CLIENT_ID and FYERS_SECRET_KEY and FYERS_ACCESS_TOKEN)
    
    # Data paths
    NSE_FNO_UNIVERSE_PATH = 'data/nse_fno_universe.json'
    
    # Screening parameters
    MAX_TICKERS = 50  # Cap for combined list
    MIN_INTRADAY_SCORE = 0  # Show all stocks
    MIN_SWING_SCORE = 0  # Show all stocks
    
    @classmethod
    def get_mode(cls):
        """Return data fetching mode"""
        return "Fyers" if cls.HAS_FYERS else "Database only"

config = Config()
