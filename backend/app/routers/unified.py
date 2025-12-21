from fastapi import APIRouter, HTTPException, BackgroundTasks
from typing import Dict, Any, List, Optional
from pydantic import BaseModel
from ..smart_trader.new_orchestrator import get_orchestrator

router = APIRouter(prefix="/api/unified", tags=["Unified Trading API"])

# Pydantic Models for API
class StandardOrderRequest(BaseModel):
    symbol: str
    action: str # BUY/SELL
    quantity: int
    product: str = "MIS"
    order_type: str = "MARKET"
    price: float = 0
    trigger_price: float = 0
    broker_params: Dict[str, Any] = {} # Extra params specific to broker

class ModeToggleRequest(BaseModel):
    mode: str # PAPER / LIVE

@router.post("/orders")
async def place_order(order: StandardOrderRequest):
    """
    Place an order through the active broker.
    Unified endpoint for any connected broker.
    """
    orchestrator = get_orchestrator()
    execution_agent = orchestrator.execution_agent
    
    # Construct signal-like object for execution agent
    # ExecutionAgent expects: signal, risk_approval
    signal = {
        "symbol": order.symbol,
        "direction": "LONG" if order.action.upper() == "BUY" else "SHORT",
        "entry_price": order.price,
        "instrument_type": "EQ", # Defaulting eq
        "reasons": ["Unified API Order"]
    }
    
    risk_approval = {
        "approved": True,
        "qty": order.quantity
    }
    
    result = execution_agent.execute_trade(signal, risk_approval)
    return result

@router.post("/toggle-mode")
async def toggle_trading_mode(req: ModeToggleRequest):
    """
    Switch the entire system between Simulation (PAPER) and LIVE trading.
    """
    mode = req.mode.upper()
    if mode not in ["PAPER", "LIVE"]:
        raise HTTPException(status_code=400, detail="Invalid mode. Use PAPER or LIVE")
    
    orchestrator = get_orchestrator()
    orchestrator.execution_agent.set_mode(mode)
    
    return {"status": "success", "mode": mode, "message": f"System switched to {mode} mode"}

@router.get("/positions")
async def get_positions():
    """Get aggregated positions from the active broker"""
    orchestrator = get_orchestrator()
    # Direct access to broker positions via adapter
    broker = orchestrator.execution_agent.broker
    if hasattr(broker, 'get_positions'):
        return broker.get_positions()
    return []

@router.get("/status")
async def get_system_status():
    """Get current system mode (Paper/Live)"""
    orchestrator = get_orchestrator()
    mode = orchestrator.execution_agent.active_mode
    return {"mode": mode, "status": "active"}
