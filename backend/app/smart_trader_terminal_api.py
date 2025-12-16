"""
Additional Smart Trader API Endpoints for Terminal Integration
"""
from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any
from datetime import datetime

# Import execution agent
from .smart_trader.execution_agent import ExecutionAgent
from .smart_trader.config import config

router = APIRouter(prefix="/api/smart-trader", tags=["smart-trader"])

# Global execution agent instance
_execution_agent = None

def get_execution_agent():
    """Get or create execution agent instance"""
    global _execution_agent
    if _execution_agent is None:
        _execution_agent = ExecutionAgent(config)
    return _execution_agent


@router.get("/positions")
async def get_positions():
    """Get all open positions from paper trading"""
    try:
        agent = get_execution_agent()
        positions = agent.get_open_positions()
        
        return {
            "success": True,
            "positions": positions
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pnl")
async def get_pnl():
    """Get total P&L from paper trading"""
    try:
        agent = get_execution_agent()
        pnl_data = agent.get_pnl_summary()
        
        return {
            "success": True,
            **pnl_data
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/close-position")
async def close_position(request: Dict[str, Any]):
    """Close a specific position"""
    try:
        trade_id = request.get('trade_id')
        if not trade_id:
            raise HTTPException(status_code=400, detail="trade_id required")
        
        agent = get_execution_agent()
        result = agent.close_position(trade_id)
        
        if result.get('success'):
            return {"success": True, "message": "Position closed", **result}
        else:
            raise HTTPException(status_code=400, detail=result.get('error', 'Failed to close position'))
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
