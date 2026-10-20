"""
Portfolio Router
================
API endpoints for portfolio data, stats, and funds.
"""

import logging
import math
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models.live_position import LivePosition

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/portfolio")
DB_DEPENDENCY = Depends(get_db)


@router.get(
    "/stats",
    response_model=None,
    responses={500: {"description": "Failed to fetch portfolio stats"}},
)
def get_portfolio_stats(db: Session = DB_DEPENDENCY) -> dict[str, Any]:
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
        logger.exception("Failed to fetch portfolio stats")
        raise HTTPException(status_code=500, detail="Failed to fetch portfolio stats") from e


FUNDS_UNAVAILABLE = "Broker funds unavailable"


@router.get(
    "/funds",
    response_model=None,
    responses={503: {"description": FUNDS_UNAVAILABLE}},
)
def get_portfolio_funds(db: Session = DB_DEPENDENCY) -> dict[str, Any]:
    """
    Get available funds/buying power from the broker.

    Fails closed: 503 when the broker client is missing, the call fails or the reply is
    not a usable funds payload, never a zero balance that looks real.
    """
    from ..services.fyers_client import get_fyers_client

    try:
        fyers = get_fyers_client()
        client = fyers.fyers if fyers else None
        funds_data = client.funds() if client else None
    except Exception:
        logger.exception("Broker funds call failed")
        funds_data = None

    if not isinstance(funds_data, dict) or funds_data.get("s") == "error":
        logger.warning("Broker funds unavailable (no client or unusable reply)")
        raise HTTPException(status_code=503, detail=FUNDS_UNAVAILABLE)
    return _funds_summary(funds_data)


# Fyers fund_limit titles and the summary field each one sets.
FUND_LIMIT_FIELDS = {
    "Total Balance": "total",
    "Available Balance": "available",
    "Utilized Amount": "used",
}


def _amount(value: object) -> float | None:
    """A funds amount as float, or None when missing or not numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if math.isfinite(amount) else None


def _funds_summary(funds_data: dict[str, Any]) -> dict[str, float]:
    """available/used/total/buyingPower from a Fyers funds reply.

    Each field is parsed on its own: one missing or malformed amount leaves only that
    field at its fallback instead of failing the whole reply.
    """
    available = _amount(funds_data.get("available")) or 0.0
    used = _amount(funds_data.get("used")) or 0.0
    summary = {
        "available": available,
        "used": used,
        "total": _amount(funds_data.get("total")) or (available + used),
    }

    # Handle complex fund_limit structure
    fund_limit = funds_data.get("fund_limit", [])
    if isinstance(fund_limit, list):
        for f in fund_limit:
            title = f.get("title") if isinstance(f, dict) else None
            field = FUND_LIMIT_FIELDS.get(title) if isinstance(title, str) else None
            amount = _amount(f.get("equityAmount")) if field else None
            if field and amount is not None:
                summary[field] = amount

    return {
        "available": summary["available"],
        "used": summary["used"],
        "total": summary["total"],
        "buyingPower": summary["available"],
    }
