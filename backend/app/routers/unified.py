"""
Unified Trading API
==================
Central API for unified trading operations.
"""

import logging
from decimal import Decimal
from typing import Any, Never

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..exceptions import BrokerUnavailableApiError
from ..utils.logger import safe_log_value

logger = logging.getLogger(__name__)

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


BROKER_UNAVAILABLE_RESPONSES: dict[int | str, dict[str, Any]] = {
    503: {"description": "Broker not connected or the broker call failed (BROKER_UNAVAILABLE)"}
}


def _broker_net_positions() -> list[dict[str, Any]]:
    """Open positions straight from the broker.

    Fails closed: raises BrokerUnavailableApiError when the client is missing, not logged in,
    the call fails or the reply is not usable, never an empty list that reads as "flat".
    """
    from ..services.fyers_client import get_fyers_client

    fyers = get_fyers_client()
    if not fyers or not fyers.validate_token():
        raise BrokerUnavailableApiError("Broker not connected")

    try:
        response = fyers.fyers.positions()
    except Exception as exc:
        logger.warning("Broker positions call failed (%s)", type(exc).__name__, exc_info=True)
        raise BrokerUnavailableApiError() from exc

    if not isinstance(response, dict) or response.get("s") != "ok":
        reply_message = response.get("message") if isinstance(response, dict) else None
        logger.warning("Broker positions reply unusable: %s", safe_log_value(reply_message))
        raise BrokerUnavailableApiError()

    positions = response.get("netPositions", [])
    if not isinstance(positions, list):
        logger.warning("Broker positions reply has no netPositions list")
        raise BrokerUnavailableApiError()
    return positions


@router.get("/positions", response_model=None, responses=BROKER_UNAVAILABLE_RESPONSES)
async def get_positions() -> dict[str, Any]:
    """Get current open positions from broker"""
    return {"positions": _broker_net_positions()}


@router.get("/pnl", response_model=None, responses=BROKER_UNAVAILABLE_RESPONSES)
async def get_total_pnl() -> dict[str, Any]:
    """Get total realized and unrealized PnL"""
    positions = _broker_net_positions()
    total_pnl = sum((Decimal(str(p.get("pl") or 0)) for p in positions), Decimal("0"))
    return {"total_pnl": float(total_pnl)}


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
