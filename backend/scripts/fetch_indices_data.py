"""
Fetch historical data for all major Indian indices from Fyers
Downloads both daily and 5-minute intraday data
"""
import sys
import os
from datetime import datetime, timedelta
import pandas as pd

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import SessionLocal, Company
from app.data_repository import DataRepository

# Fyers symbols for indices
INDICES_SYMBOLS = {
    "NIFTY50": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "NIFTY100": "NSE:NIFTY100-INDEX",
    "NIFTY200": "NSE:NIFTY200-INDEX",
    "MIDCAPNIFTY": "NSE:NIFTYMIDCAP50-INDEX",
    "SMALLCAPNIFTY": "NSE:NIFTYSMALLCAP50-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "NIFTYIT": "NSE:NIFTYIT-INDEX",
    "NIFTYPHARMA": "NSE:NIFTYPHARMA-INDEX",
    "NIFTYAUTO": "NSE:NIFTYAUTO-INDEX",
    "NIFTYMETAL": "NSE:NIFTYMETAL-INDEX",
    "NIFTYREALTY": "NSE:NIFTYREALTY-INDEX",
    "NIFTYFMCG": "NSE:NIFTYFMCG-INDEX",
    "NIFTYENERGY": "NSE:NIFTYENERGY-INDEX",
}

def fetch_index_historical(fyers_client, symbol: str, fyers_symbol: str, days: int = 365):
    """Fetch daily historical data for an index"""
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        print(f"📊 Fetching daily data for {symbol}...")
        response = fyers_client.get_historical_data(
            symbol=fyers_symbol,
            timeframe="1D",
            range_from=start_date.strftime("%Y-%m-%d"),
            range_to=end_date.strftime("%Y-%m-%d")
        )
        
        if response.get('s') == 'ok' and 'candles' in response:
            candles = response['candles']
            df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['date'] = pd.to_datetime(df['timestamp'], unit='s')
            df = df.set_index('date')
            df = df[['open', 'high', 'low', 'close', 'volume']]
            print(f"✅ Downloaded {len(df)} daily candles for {symbol}")
            return df
        else:
            print(f"❌ Failed: {response}")
            return None
    except Exception as e:
        print(f"❌ Error fetching {symbol}: {e}")
        return None

def fetch_index_intraday(fyers_client, symbol: str, fyers_symbol: str, days: int = 5):
    """Fetch 5-minute intraday data for an index"""
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        print(f"📈 Fetching 5min data for {symbol}...")
        response = fyers_client.get_historical_data(
            symbol=fyers_symbol,
            timeframe="5",
            range_from=start_date.strftime("%Y-%m-%d"),
            range_to=end_date.strftime("%Y-%m-%d")
        )
        
        if response.get('s') == 'ok' and 'candles' in response:
            candles = response['candles']
            df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='s')
            df = df.set_index('datetime')
            df = df[['open', 'high', 'low', 'close', 'volume']]
            print(f"✅ Downloaded {len(df)} 5min candles for {symbol}")
            return df
        else:
            print(f"❌ Failed: {response}")
            return None
    except Exception as e:
        print(f"❌ Error fetching {symbol} intraday: {e}")
        return None

    """Save dataframe to CSV"""
    if timeframe == '5min':
        # Save intraday to nse_data/raw/intraday
        data_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'nse_data', 'raw', 'intraday'))
    else:
        # Save daily to nse_data/raw/indices
        data_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'nse_data', 'raw', 'indices'))
        
    os.makedirs(data_dir, exist_ok=True)
    
    filename = f"{symbol}_{timeframe}.csv"
    filepath = os.path.join(data_dir, filename)
    
    df.to_csv(filepath)
    print(f"💾 Saved to {filepath}")

def main():
    """Main function to fetch all indices data"""
    print("=" * 60)
    print("INDEX DATA DOWNLOADER - Fyers API")
    print("=" * 60)
    
    # Initialize Fyers
    try:
        # Import fyers from project root
        algotrading_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
        if algotrading_root in sys.path:
            sys.path.remove(algotrading_root)
        sys.path.insert(0, algotrading_root)
        
        from fyers import fyers_client
        print("✅ Fyers client initialized")
    except Exception as e:
        print(f"❌ Failed to initialize Fyers: {e}")
        print("Please ensure you're logged in to Fyers (cd fyers && python fyers_login.py)")
        return
    
    # Fetch data for all indices
    for symbol, fyers_symbol in INDICES_SYMBOLS.items():
        print(f"\n{'='*60}")
        print(f"Processing {symbol}")
        print(f"{'='*60}")
        
        # Fetch daily data (1 year)
        daily_df = fetch_index_historical(fyers_client, symbol, fyers_symbol, days=365)
        if daily_df is not None:
            save_to_csv(daily_df, symbol, 'daily')
        
        # Fetch 5min data (5 days)
        intraday_df = fetch_index_intraday(fyers_client, symbol, fyers_symbol, days=5)
        if intraday_df is not None:
            save_to_csv(intraday_df, symbol, '5min')
    
    print("\n" + "=" * 60)
    print("✅ COMPLETED - All indices data downloaded!")
    print("=" * 60)

if __name__ == "__main__":
    main()
