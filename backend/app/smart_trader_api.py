"""
Smart Trader API Endpoints - Updated for new architecture
"""
from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any
import json

# Import new orchestrator
from .smart_trader.new_orchestrator import get_orchestrator
from .database import SessionLocal, SmartTraderSignal

router = APIRouter(prefix="/api/smart-trader", tags=["smart-trader"])


@router.post("/start")
async def start_scanner():
    """Start the Smart Trader scanner"""
    try:
        orchestrator = get_orchestrator()
        orchestrator.start_market_session()
        return {"success": True, "message": "Scanner started"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stop")
async def stop_scanner():
    """Stop the Smart Trader scanner"""
    try:
        orchestrator = get_orchestrator()
        orchestrator.stop_market_session()
        return {"success": True, "message": "Scanner stopped"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals")
async def get_signals(
    confidence_level: str = None,
    signal_family: str = None,
    limit: int = 50
):
    """
    Get current signals with optional filtering
    
    Args:
        confidence_level: Filter by LOW, MEDIUM, or HIGH
        signal_family: Filter by signal family (MOMENTUM, VOLUME, etc.)
        limit: Maximum number of signals to return
    """
    try:
        orchestrator = get_orchestrator()
        signals = orchestrator.get_current_signals()
        
        # Apply filters
        if confidence_level:
            signals = [s for s in signals if s['confidence_level'] == confidence_level.upper()]
        
        if signal_family:
            signals = [s for s in signals if signal_family.upper() in s['signal_families']]
        
        # Limit results
        signals = signals[:limit]
        
        return {
            "success": True,
            "count": len(signals),
            "signals": signals
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals/{signal_id}")
async def get_signal_detail(signal_id: str):
    """Get detailed information about a specific signal"""
    try:
        orchestrator = get_orchestrator()
        signals = orchestrator.get_current_signals()
        
        signal = next((s for s in signals if s['id'] == signal_id), None)
        if not signal:
            raise HTTPException(status_code=404, detail="Signal not found")
        
        return {"success": True, "signal": signal}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/execute/{signal_id}")
async def execute_trade(signal_id: str):
    """Execute a trade for a specific signal"""
    try:
        orchestrator = get_orchestrator()
        result = orchestrator.execute_trade(signal_id)
        
        if not result.get('success'):
            raise HTTPException(status_code=400, detail=result.get('error', 'Trade execution failed'))
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
async def get_system_status():
    """Get Smart Trader system status"""
    try:
        orchestrator = get_orchestrator()
        
        return {
            "success": True,
            "is_running": orchestrator.is_running,
            "signal_count": len(orchestrator.current_signals),
            "generators": [g.__class__.__name__ for g in orchestrator.generators]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/scan")
async def trigger_scan():
    """Manually trigger a scan cycle"""
    try:
        orchestrator = get_orchestrator()
        orchestrator._scan_cycle()
        
        return {
            "success": True,
            "message": "Scan cycle completed",
            "signal_count": len(orchestrator.current_signals)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
