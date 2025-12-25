"""
Data cache manager for historical data persistence
"""
import json
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Optional
import pandas as pd
import shutil

class DataCache:
    def __init__(self, cache_dir: str = 'cache'):
        self.root_dir = Path(__file__).parent.parent
        self.cache_dir = self.root_dir / cache_dir
        self.historical_data_dir = self.cache_dir / 'historical_data'
        
        # Ensure directories exist
        self.cache_dir.mkdir(exist_ok=True)
        self.historical_data_dir.mkdir(exist_ok=True)
        
        self.cache_metadata_file = self.cache_dir / 'cache_metadata.json'
        
    def get_cache_metadata(self) -> Dict:
        """Get cache metadata (last update time, etc.)"""
        try:
            if self.cache_metadata_file.exists():
                with open(self.cache_metadata_file, 'r') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}
    
    def update_cache_metadata(self, metadata: Dict):
        """Update cache metadata"""
        with open(self.cache_metadata_file, 'w') as f:
            json.dump(metadata, f, indent=2)
    
    def get_symbol_file(self, symbol: str) -> Path:
        """Get path to symbol's data file"""
        return self.historical_data_dir / f"{symbol}.json"

    def is_cache_valid(self, symbol: str) -> bool:
        """Check if cached data is still valid for a symbol"""
        metadata = self.get_cache_metadata()
        if symbol not in metadata:
            return False
        
        # Check if file actually exists
        if not self.get_symbol_file(symbol).exists():
            return False
        
        last_update = datetime.fromisoformat(metadata[symbol]['last_update'])
        today = datetime.now().date()
        
        # Cache is valid if:
        # 1. Last update was today (during market hours)
        # 2. Last update was yesterday after market close (before market open today)
        if last_update.date() == today:
            return True
        
        # If it's before 9:15 AM and cache is from yesterday, it's still valid
        now = datetime.now()
        if now.hour < 9 or (now.hour == 9 and now.minute < 15):
            if last_update.date() == today - timedelta(days=1):
                return True
        
        return False
    
    def save_historical_data(self, symbol: str, data: pd.DataFrame):
        """Save historical data to cache (one file per symbol)"""
        if data is None or data.empty:
            return

        # Prepare content
        content = {
            'symbol': symbol,
            'last_update': datetime.now().isoformat(),
            'rows': len(data),
            'data': data.to_dict('records'),
            'index': data.index.astype(str).tolist()
        }
        
        # Save to individual file
        file_path = self.get_symbol_file(symbol)
        with open(file_path, 'w') as f:
            json.dump(content, f)
        
        # Update shared metadata
        metadata = self.get_cache_metadata()
        metadata[symbol] = {
            'last_update': datetime.now().isoformat(),
            'rows': len(data)
        }
        self.update_cache_metadata(metadata)
    
    def load_historical_data(self, symbol: str) -> Optional[pd.DataFrame]:
        """Load historical data from cache"""
        file_path = self.get_symbol_file(symbol)
        
        if not file_path.exists():
            return None
        
        try:
            with open(file_path, 'r') as f:
                content = json.load(f)
            
            # Reconstruct DataFrame
            df = pd.DataFrame(content['data'])
            if 'index' in content and content['index']:
                df.index = pd.to_datetime(content['index'])
                df.index.name = 'Date'
            
            return df
        except Exception as e:
            print(f"Error loading cache for {symbol}: {e}")
            return None
    
    def append_today_candle(self, symbol: str, candle: Dict) -> Optional[pd.DataFrame]:
        """Append today's candle to cached historical data"""
        df = self.load_historical_data(symbol)
        if df is None:
            return None
        
        # Create new row
        today = datetime.now().date()
        new_row = pd.DataFrame([candle], index=[pd.Timestamp(today)])
        
        # Check if today's candle already exists
        if today in df.index.date:
            # Update existing row
            # Note: Indexing with date might return multiple rows if not unique, 
            # but here we assume daily candles.
            mask = df.index.date == today
            df.loc[mask] = new_row.values[0] # safer assignment
        else:
            # Append new row
            df = pd.concat([df, new_row])
        
        # Save updated data
        self.save_historical_data(symbol, df)
        
        return df
    
    def clear_cache(self):
        """Clear all cached data"""
        try:
            # Remove metadata
            if self.cache_metadata_file.exists():
                self.cache_metadata_file.unlink()
            
            # Remove historical data directory contents
            if self.historical_data_dir.exists():
                shutil.rmtree(self.historical_data_dir)
                self.historical_data_dir.mkdir()
                
        except Exception as e:
            print(f"Error clearing cache: {e}")

    def delete_symbol_cache(self, symbol: str) -> bool:
        """Delete specific symbol cache"""
        file_path = self.get_symbol_file(symbol)
        if file_path.exists():
            file_path.unlink()
            
            # Update metadata
            metadata = self.get_cache_metadata()
            if symbol in metadata:
                del metadata[symbol]
                self.update_cache_metadata(metadata)
            return True
        return False

# Global cache instance
cache_manager = DataCache()
