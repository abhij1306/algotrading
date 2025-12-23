"""
Pre-flight health check endpoint for Screener
Verifies Fyers connectivity and data freshness before UI loads
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from ..database import get_db
from ..data_layer.provider import DataProvider

router = APIRouter(prefix="/api/system", tags=["System Health"])


@router.get("/health")
def system_health_check(db: Session = Depends(get_db)):
    """
    Comprehensive system health check
    
    Returns:
        {
            "status": "READY" | "DEGRADED" | "DOWN",
            "postgresql": "OK" | "DOWN",
            "fyers": "OK" | "TOKEN_EXPIRED" | "DOWN",
            "last_data_update": ISO timestamp,
            "total_symbols": int,
            "recommendations": [str]
        }
    """
    provider = DataProvider(db)
    health = provider.health_check()
    
    # Determine overall status
    status = "READY"
    recommendations = []
    
    if health["postgresql"] != "OK":
        status = "DOWN"
        recommendations.append("PostgreSQL database is unavailable. Check connection.")
    
    if health["fyers"] == "TOKEN_EXPIRED":
        status = "DEGRADED"
        recommendations.append("Fyers access token expired. Re-authenticate at /api/auth/fyers")
    elif health["fyers"] != "OK":
        status = "DEGRADED"
        recommendations.append("Fyers API unavailable. Screener will use cached data only.")
    
    if health["last_update"] is None:
        recommendations.append("No historical data found. Run daily_maintenance scripts.")
    
    return {
        "status": status,
        **health,
        "recommendations": recommendations
    }


@router.get("/pre-flight/screener")
def screener_pre_flight(db: Session = Depends(get_db)):
    """
    Specific pre-flight check for Screener module
    Called before Screener UI loads to ensure data availability
    
    Returns:
        {
            "ready": bool,
            "message": str,
            "fyers_connected": bool,
            "data_fresh": bool,
            "last_update": str
        }
    """
    provider = DataProvider(db)
    health = provider.health_check()
    
    fyers_ok = health["fyers"] == "OK"
    data_exists = health["total_symbols"] > 0
    
    if not data_exists:
        return {
            "ready": False,
            "message": "No market data available. Please run data initialization scripts.",
            "fyers_connected": fyers_ok,
            "data_fresh": False,
            "last_update": None
        }
    
    if not fyers_ok:
        return {
            "ready": True,  # Can still use cached data
            "message": "Fyers connection unavailable. Using cached data only.",
            "fyers_connected": False,
            "data_fresh": False,
            "last_update": health["last_update"]
        }
    
    return {
        "ready": True,
        "message": "All systems operational",
        "fyers_connected": True,
        "data_fresh": True,
        "last_update": health["last_update"]
    }
