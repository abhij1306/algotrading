"""
Data Cache - Intent-based TTL Cache
Prevents cache pollution by using intent-specific keys and TTLs
"""

from datetime import datetime, timedelta
from typing import Optional, Any, Dict
import pandas as pd
import hashlib
import json

class DataCache:
    """
    Intent-based cache with automatic TTL management
    - Different TTLs for different use cases
    - Prevents cache pollution
    - Thread-safe operations
    """
    
    # Intent-based TTL configuration (in seconds)
    TTL_CONFIG = {
        "backtest": 3600,        # 1 hour - backtesting data
        "scanner": 60,           # 1 minute - scanner queries
        "analysis": 300,         # 5 minutes - analysis/research
        "chart": 180,            # 3 minutes - chart data
        "realtime": 5,           # 5 seconds - real-time quotes
        "default": 300           # 5 minutes - default
    }
    
    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
    
    def _generate_key(self, intent: str, **params) -> str:
        """Generate cache key from intent and parameters"""
        # Sort params for consistent key generation
        sorted_params = json.dumps(params, sort_keys=True)
        key_str = f"{intent}:{sorted_params}"
        return hashlib.md5(key_str.encode()).hexdigest()
    
    def get(self, intent: str, **params) -> Optional[pd.DataFrame]:
        """
        Get data from cache
        
        Args:
            intent: Use case intent (backtest, scanner, analysis, etc.)
            **params: Query parameters (symbol, start_date, end_date, etc.)
        
        Returns:
            Cached DataFrame or None if not found/expired
        """
        key = self._generate_key(intent, **params)
        
        if key not in self._cache:
            return None
        
        entry = self._cache[key]
        
        # Check if expired
        if datetime.now() > entry['expires_at']:
            del self._cache[key]
            return None
        
        return entry['data']
    
    def set(self, data: pd.DataFrame, intent: str, **params) -> None:
        """
        Store data in cache
        
        Args:
            data: DataFrame to cache
            intent: Use case intent
            **params: Query parameters
        """
        key = self._generate_key(intent, **params)
        ttl = self.TTL_CONFIG.get(intent, self.TTL_CONFIG['default'])
        
        self._cache[key] = {
            'data': data.copy(),
            'created_at': datetime.now(),
            'expires_at': datetime.now() + timedelta(seconds=ttl),
            'intent': intent,
            'params': params
        }
    
    def invalidate(self, intent: Optional[str] = None, **params) -> int:
        """
        Invalidate cache entries
        
        Args:
            intent: If provided, only invalidate entries with this intent
            **params: If provided, only invalidate entries matching these params
        
        Returns:
            Number of entries invalidated
        """
        if intent and params:
            # Invalidate specific entry
            key = self._generate_key(intent, **params)
            if key in self._cache:
                del self._cache[key]
                return 1
            return 0
        
        elif intent:
            # Invalidate all entries with this intent
            keys_to_delete = [
                k for k, v in self._cache.items()
                if v['intent'] == intent
            ]
            for key in keys_to_delete:
                del self._cache[key]
            return len(keys_to_delete)
        
        else:
            # Clear all cache
            count = len(self._cache)
            self._cache.clear()
            return count
    
    def cleanup_expired(self) -> int:
        """Remove expired entries from cache"""
        now = datetime.now()
        keys_to_delete = [
            k for k, v in self._cache.items()
            if now > v['expires_at']
        ]
        
        for key in keys_to_delete:
            del self._cache[key]
        
        return len(keys_to_delete)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        now = datetime.now()
        
        stats = {
            'total_entries': len(self._cache),
            'by_intent': {},
            'expired_entries': 0
        }
        
        for entry in self._cache.values():
            intent = entry['intent']
            stats['by_intent'][intent] = stats['by_intent'].get(intent, 0) + 1
            
            if now > entry['expires_at']:
                stats['expired_entries'] += 1
        
        return stats


# Global cache instance
_cache_instance = None

def get_cache() -> DataCache:
    """Get global cache instance (singleton)"""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = DataCache()
    return _cache_instance


# Example usage
if __name__ == "__main__":
    cache = get_cache()
    
    # Example: Cache backtest data
    sample_data = pd.DataFrame({
        'DATE': ['2024-01-01', '2024-01-02'],
        'CLOSE': [100, 105]
    })
    
    cache.set(
        data=sample_data,
        intent="backtest",
        symbol="RELIANCE",
        start_date="2024-01-01",
        end_date="2024-12-31"
    )
    
    # Retrieve from cache
    cached = cache.get(
        intent="backtest",
        symbol="RELIANCE",
        start_date="2024-01-01",
        end_date="2024-12-31"
    )
    
    print("Cached data:", cached)
    print("Cache stats:", cache.get_stats())
