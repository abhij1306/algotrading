from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from typing import Dict, Any, List
from ..database import get_db, ActionCenter, SessionLocal
from ..smart_trader.execution_agent import ExecutionAgent
from ..smart_trader.config import config

router = APIRouter(
    prefix="/api/actions",
    tags=["Action Center"]
)

@router.get("/pending")
def get_pending_actions(db: Session = Depends(get_db)):
    """List all pending actions"""
    actions = db.query(ActionCenter).filter(ActionCenter.status == 'PENDING').order_by(ActionCenter.created_at.desc()).all()
    return [{
        "id": a.id,
        "source": a.source_agent,
        "type": a.action_type,
        "symbol": a.payload.get('symbol'),
        "details": a.payload,
        "reason": a.reason,
        "confidence": a.confidence,
        "created_at": a.created_at
    } for a in actions]

@router.post("/{action_id}/approve")
def approve_action(action_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Approve and Execute Action"""
    action = db.query(ActionCenter).filter(ActionCenter.id == action_id).first()
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")
        
    if action.status != 'PENDING':
        raise HTTPException(status_code=400, detail="Action already processed")

    # Update status immediately
    action.status = 'APPROVED'
    db.commit()
    
    # Execute in background
    background_tasks.add_task(execute_action_task, action_id)
    
    return {"status": "success", "message": "Action approved and scheduled for execution"}

@router.post("/{action_id}/reject")
def reject_action(action_id: int, db: Session = Depends(get_db)):
    """Reject Action"""
    action = db.query(ActionCenter).filter(ActionCenter.id == action_id).first()
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")
        
    action.status = 'REJECTED'
    db.commit()
    return {"status": "success", "message": "Action rejected"}

def execute_action_task(action_id: int):
    """Background task to execute the approved action"""
    db = SessionLocal()
    try:
        action = db.query(ActionCenter).filter(ActionCenter.id == action_id).first()
        if not action: return
        
        # Initialize Agent
        agent = ExecutionAgent(config)
        
        # Execute based on Type
        if action.action_type == 'PLACE_ORDER':
            payload = action.payload
            
            # Construct Signal & Approval
            signal = {
                'symbol': payload['symbol'],
                'direction': payload['direction'],
                'entry_price': payload.get('entry_price', 0)
            }
            risk_approval = {
                'approved': True, 
                'qty': payload['quantity']
            }
            
            print(f"[ACTION CENTER] Executing Approved Order: {signal['symbol']}")
            result = agent.execute_trade(signal, risk_approval)
            
            if result['status'] == 'FILLED':
                action.status = 'EXECUTED'
            else:
                action.status = 'FAILED'
                action.reason = f"Execution Failed: {result.get('message')}"
                
            db.commit()
            
    except Exception as e:
        print(f"[ACTION CENTER] Execution Error: {e}")
        action.status = 'FAILED'
        action.reason = str(e)
        db.commit()
    finally:
        db.close()
