"""
Cache management API endpoints
"""
from fastapi import APIRouter
from .cache_manager import cache_manager

router = APIRouter()

@router.get("/cache/status")
def get_cache_status():
    """Get cache status and metadata"""
    metadata = cache_manager.get_cache_metadata()
    return {
        "total_symbols": len(metadata),
        "symbols": list(metadata.keys()),
        "metadata": metadata
    }

@router.post("/cache/clear")
def clear_cache():
    """Clear all cached data"""
    cache_manager.clear_cache()
    return {"message": "Cache cleared successfully"}

@router.delete("/cache/{symbol}")
def delete_symbol_cache(symbol: str):
    """Delete cache for a specific symbol"""
    if cache_manager.delete_symbol_cache(symbol):
        return {"message": f"Cache for {symbol} invalidated"}
    return {"message": f"No cache found for {symbol}"}
