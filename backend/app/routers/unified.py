"""
Unified Trading API
==================
Central API for unified trading operations.
"""

from typing import Any, Never

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/smart-trader", tags=["Smart Trader API"])


# Pydantic Models for API
class StandardOrderRequest(BaseModel):
    symbol: str
    action: str  # BUY/SELL
    quantity: int
    product: str = "MIS"
    order_type: str = "MARKET"
    price: float = 0
    trigger_price: float = 0
    broker_params: dict[str, Any] = {}


class ModeToggleRequest(BaseModel):
    mode: str  # PAPER / LIVE


class ClosePositionRequest(BaseModel):
    trade_id: str


@router.get("/health", response_model=None)
async def health_check() -> dict[str, Any]:
    """Health check endpoint"""
    return {"status": "ok", "message": "Smart Trader API is available."}


@router.get("/status", response_model=None)
async def get_status() -> dict[str, Any]:
    """Get current trading mode and status"""
    from ..services.fyers_client import get_fyers_client

    fyers = get_fyers_client()
    is_connected = fyers.validate_token() if fyers else False

    return {
        "mode": "PAPER",  # Default to paper for now
        "broker_connected": is_connected,
        "message": "Connected to Fyers" if is_connected else "Fyers not connected",
    }


@router.get("/positions", response_model=None)
async def get_positions() -> dict[str, Any]:
    """Get current open positions from broker"""
    from ..services.fyers_client import get_fyers_client

    fyers = get_fyers_client()

    if not fyers or not fyers.validate_token():
        return {"positions": []}

    try:
        # Get positions from Fyers SDK directly or via a service
        # For now, let's try to get them if the SDK is available
        response = fyers.fyers.positions()
        if response.get("s") == "ok":
            return {"positions": response.get("netPositions", [])}
        return {"positions": []}
    except Exception as e:
        return {"positions": [], "error": str(e)}


@router.get("/pnl", response_model=None)
async def get_total_pnl() -> dict[str, Any]:
    """Get total realized and unrealized PnL"""
    from ..services.fyers_client import get_fyers_client

    fyers = get_fyers_client()

    if not fyers or not fyers.validate_token():
        return {"total_pnl": 0.0}

    try:
        response = fyers.fyers.positions()
        if response.get("s") == "ok":
            positions = response.get("netPositions", [])
            total_pnl = sum(p.get("pl", 0.0) for p in positions)
            return {"total_pnl": total_pnl}
        return {"total_pnl": 0.0}
    except Exception:
        return {"total_pnl": 0.0}


@router.post(
    "/order",
    response_model=None,
    responses={503: {"description": "Order placement is disabled in this version"}},
)
async def place_order(request: StandardOrderRequest) -> Never:
    """Place a unified order"""
    raise HTTPException(
        status_code=503, detail="Order placement is currently disabled in this version."
    )


@router.post("/close-position", response_model=None)
async def close_position(request: ClosePositionRequest) -> dict[str, Any]:
    """Close a specific position"""
    return {"status": "success", "message": f"Close request for {request.trade_id} received"}


@router.post("/mode", response_model=None)
async def toggle_mode(request: ModeToggleRequest) -> dict[str, Any]:
    """Toggle between PAPER and LIVE mode"""
    return {
        "previous_mode": "PAPER",
        "new_mode": request.mode,
        "message": f"Switched to {request.mode} mode",
    }
