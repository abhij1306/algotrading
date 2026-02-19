"""
Portfolio Router
================
API endpoints for portfolio data, stats, and funds.
"""


from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models.live_position import LivePosition

router = APIRouter(prefix="/api/portfolio")
DB_DEPENDENCY = Depends(get_db)


@router.get("/stats")
def get_portfolio_stats(db: Session = DB_DEPENDENCY):
    """
    Get portfolio statistics including total value, P&L, and returns.
    """
    try:
        # Get all positions
        positions = db.query(LivePosition).filter(LivePosition.net_qty != 0).all()

        total_value = 0.0
        day_change = 0.0
        total_invested = 0.0

        for pos in positions:
            position_value = abs(pos.net_qty) * pos.ltp
            total_value += position_value

            # Calculate invested amount based on side
            if pos.net_qty > 0:  # Long
                invested = pos.net_qty * pos.net_avg
            else:  # Short
                invested = abs(pos.net_qty) * pos.net_avg

            total_invested += invested

            # Day change calculation (using unrealized P&L as proxy)
            day_change += pos.unrealized_pl or 0

        # Calculate returns
        total_return = total_value - total_invested if total_invested > 0 else 0
        total_return_percent = (total_return / total_invested * 100) if total_invested > 0 else 0
        day_change_percent = (day_change / total_value * 100) if total_value > 0 else 0

        return {
            "totalValue": total_value,
            "dayChange": day_change,
            "dayChangePercent": round(day_change_percent, 2),
            "totalReturn": total_return,
            "totalReturnPercent": round(total_return_percent, 2),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch portfolio stats: {str(e)}")


@router.get("/funds")
def get_portfolio_funds(db: Session = DB_DEPENDENCY):
    """
    Get available funds/buying power.
    """
    try:
        # Try to get from broker
        from ..services.fyers_client import get_fyers_client
        fyers = get_fyers_client()

        if fyers and fyers.fyers:
            funds_data = fyers.fyers.funds()
            if isinstance(funds_data, dict):
                available = float(funds_data.get("available", 0) or 0)
                used = float(funds_data.get("used", 0) or 0)
                total = float(funds_data.get("total", 0) or (available + used))

                # Handle complex fund_limit structure
                fund_limit = funds_data.get("fund_limit", [])
                if isinstance(fund_limit, list):
                    for f in fund_limit:
                        if f.get("title") == "Total Balance":
                            total = float(f.get("equityAmount", 0))
                        if f.get("title") == "Available Balance":
                            available = float(f.get("equityAmount", 0))
                        if f.get("title") == "Utilized Amount":
                            used = float(f.get("equityAmount", 0))

                return {
                    "available": available,
                    "used": used,
                    "total": total,
                    "buyingPower": available
                }

        # Fallback: return zeros if broker not available
        return {
            "available": 0,
            "used": 0,
            "total": 0,
            "buyingPower": 0
        }
    except Exception:
        # Return zeros on error
        return {
            "available": 0,
            "used": 0,
            "total": 0,
            "buyingPower": 0
        }
