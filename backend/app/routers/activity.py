"""
Activity Router
===============
API endpoints for recent activity and history.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db

router = APIRouter(prefix="/api/activity")
DB_DEPENDENCY = Depends(get_db)


@router.get("/recent")
def get_recent_activity(db: Session = DB_DEPENDENCY):
    """
    Get recent trading activity (orders, trades, dividends).
    """
    try:
        from ..models.live_order import LiveOrder

        activities = []

        # Get recent orders (last 24 hours)
        yesterday = datetime.now() - timedelta(days=1)
        recent_orders = db.query(LiveOrder).filter(
            LiveOrder.created_at >= yesterday
        ).order_by(LiveOrder.created_at.desc()).limit(10).all()

        for order in recent_orders:
            if order.status == "FILLED":
                activities.append({
                    "type": order.side.lower() if order.side else "buy",
                    "symbol": order.symbol,
                    "quantity": order.quantity,
                    "price": order.price or 0,
                    "time": order.created_at.strftime("%H:%M") if order.created_at else "--"
                })

        # Sort by time and take most recent
        activities.sort(key=lambda x: x["time"], reverse=True)

        # If no activities, return empty array
        if not activities:
            return []

        return activities[:5]  # Return last 5 activities

    except Exception:
        # Return empty on error
        return []
