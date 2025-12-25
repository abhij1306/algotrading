"""
Screener Router - Module-based API endpoints
Provides stock screening with technical/fundamental filters
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional, List
import sys
from pathlib import Path

# Add parent directory to path
sys.path.append(str(Path(__file__).parent.parent.parent.parent))

from app.database import get_db
from ..services.screener import get_screener_results
from ..services.scoring import calculate_scores

router = APIRouter(prefix="/api/screener", tags=["screener"])

@router.get("/")
async def screener_endpoint(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    index: str = Query("NIFTY50"),
    filter_type: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    sector: Optional[str] = Query(None),
    view: str = Query("technical"),
    db: Session = Depends(get_db)
):
    """
    Main screener endpoint with pagination and filters
    
    Args:
        page: Page number (1-indexed)
        limit: Results per page
        index: Index filter (NIFTY50, NIFTY500, etc.)
        filter_type: Preset filter (VOLUME_SHOCKER, PRICE_SHOCKER, etc.)
        symbol: Filter by specific symbol
        sector: Filter by sector
        view: View mode (technical or financial)
        db: Database session
    
    Returns:
        {
            "data": [...],
            "meta": {"total": X, "page": Y, "limit": Z}
        }
    """
    try:
        results = await get_screener_results(
            db=db,
            page=page,
            limit=limit,
            index_filter=index,
            filter_type=filter_type,
            symbol_filter=symbol,
            sector_filter=sector,
            view_mode=view
        )
        
        return {
            "data": results["stocks"],
            "meta": {
                "total": results.get("total", len(results["stocks"])),
                "page": page,
                "limit": limit
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/indices")
async def get_available_indices():
    """Get list of available indices for filtering"""
    from app.constants.indices import STOCK_INDICES
    
    indices = [
        {
            "id": key,
            "name": info.get("name", key),
            "symbol": info.get("symbol", key)
        }
        for key, info in STOCK_INDICES.items()
    ]
    
    return {
        "indices": indices,
        "default": "NIFTY50"
    }
