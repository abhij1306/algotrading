"""
Live Price Service for Smart Trader
Fetches and caches live prices from Fyers API for scanner
"""
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import sys
import os

# Add AlgoTrading root to path for fyers import
sys.path.insert(0, r'C:\AlgoTrading')

class LivePriceService:
    """Service to fetch and cache live prices for scanner"""
    
    def __init__(self):
        self.cache = {}
        self.last_update = None
        self.cache_ttl = 30  # Cache for 30 seconds
    
    def get_live_prices(self, symbols: List[str]) -> Dict[str, Dict]:
        """
        Fetch live prices for multiple symbols
        
        Args:
            symbols: List of stock symbols (without NSE: prefix)
            
        Returns:
            Dict with format: {symbol: {ltp, change_pct, volume, high, low}}
        """
        # Check if cache is still valid
        if self._is_cache_valid():
            # Return cached prices for requested symbols
            return {sym: self.cache.get(sym, {}) for sym in symbols if sym in self.cache}
        
        # Fetch fresh prices
        try:
            from backend.app.fyers_direct import get_fyers_quotes
            
            print(f"[LIVE PRICE SERVICE] Fetching live prices for {len(symbols)} symbols...")
            quotes = get_fyers_quotes(symbols)
            
            if quotes:
                # Update cache
                self.cache = quotes
                self.last_update = datetime.now()
                print(f"[LIVE PRICE SERVICE] Cached {len(quotes)} live prices")
                return quotes
            else:
                print("[LIVE PRICE SERVICE] No quotes returned, using cache")
                return {sym: self.cache.get(sym, {}) for sym in symbols if sym in self.cache}
                
        except Exception as e:
            print(f"[LIVE PRICE SERVICE] Error fetching live prices: {e}")
            # Return cached data if available
            return {sym: self.cache.get(sym, {}) for sym in symbols if sym in self.cache}
    
    def get_single_price(self, symbol: str) -> Optional[Dict]:
        """Get live price for a single symbol"""
        prices = self.get_live_prices([symbol])
        return prices.get(symbol)
    
    def _is_cache_valid(self) -> bool:
        """Check if cache is still valid"""
        if not self.last_update or not self.cache:
            return False
        
        age = (datetime.now() - self.last_update).total_seconds()
        return age < self.cache_ttl
    
    def clear_cache(self):
        """Clear the price cache"""
        self.cache = {}
        self.last_update = None
        print("[LIVE PRICE SERVICE] Cache cleared")


# Global instance
_live_price_service = None

def get_live_price_service() -> LivePriceService:
    """Get or create global live price service instance"""
    global _live_price_service
    
    if _live_price_service is None:
        _live_price_service = LivePriceService()
    
    return _live_price_service
