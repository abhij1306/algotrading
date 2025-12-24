from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from ..database import get_db
from ..services.market_data_service import MarketDataService

router = APIRouter(prefix="/market", tags=["Market Dashboard"])

@router.get("/overview")
def get_market_overview():
    """
    Returns a comprehensive market overview including:
    - Global Indices (US, India, etc.)
    - Commodities (Gold, Silver)
    - Market Sentiment (Fear & Greed, India VIX)
    - Technical Market Condition (Trend/Range)
    """
    try:
        # Fetch data in parallel conceptually (synchronous calls for now)
        indices = MarketDataService.get_global_indices()
        sentiment = MarketDataService.get_market_sentiment()
        condition = MarketDataService.get_market_condition()
        
        return {
            "indices": indices,
            "sentiment": sentiment,
            "condition": condition,
            "timestamp": "now"
        }
    except Exception as e:
        return {"error": str(e)}

@router.get("/sentiment")
def get_market_sentiment_endpoint(db: Session = Depends(get_db)):
    """
    Legacy/Specific sentiment endpoint if needed, or mapped to new service.
    """
    return MarketDataService.get_market_sentiment()

@router.get("/indices")
def get_index_performance():
    """
    Legacy/Specific indices endpoint.
    """
    return MarketDataService.get_global_indices()
