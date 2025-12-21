from datetime import datetime
from typing import Dict, Any, Optional
import json
from ..database import SessionLocal, AgentAuditLog

class AuditLogger:
    """
    Centralized logging for Agent Actions and Broker API calls.
    Persists to AgentAuditLog table.
    """
    
    @staticmethod
    def log_decision(
        agent_name: str,
        action_type: str,
        symbol: Optional[str],
        input_snapshot: Dict,
        decision: Dict,
        reasoning: str,
        confidence: float,
        status: str = 'SUCCESS'
    ):
        """Log a high-level agent decision (e.g., Signal Generated, Trade Executed)"""
        db = SessionLocal()
        try:
            log = AgentAuditLog(
                agent_name=agent_name,
                action_type=action_type,
                symbol=symbol,
                input_snapshot=input_snapshot, # SQLAlchemy JSON handles dict
                decision=decision,
                reasoning=reasoning,
                confidence=confidence,
                status=status,
                execution_time_ms=0 # Todo: measure time
            )
            db.add(log)
            db.commit()
        except Exception as e:
            print(f"❌ [AUDIT FAIL] {e}")
        finally:
            db.close()

    @staticmethod
    def log_api_call(
        broker_name: str,
        method: str,
        params: Dict,
        response: Any,
        duration_ms: int,
        error: str = None
    ):
        """Log a broker API call (Simulated or Real)"""
        # For API logs, we might want a separate table 'SystemLogs' or use AuditLog with specific types
        # Using AgentAuditLog with type="BROKER_API"
        
        status = 'FAILURE' if error else 'SUCCESS'
        reason = error if error else 'API Call Success'
        
        # Sanitize Params (Remove Secrets if any)
        # ...
        
        AuditLogger.log_decision(
            agent_name=f"BROKER::{broker_name}",
            action_type=f"API::{method}",
            symbol=params.get('symbol'),
            input_snapshot=params,
            decision={"response": str(response)}, # Store string repr if complex
            reasoning=f"Duration: {duration_ms}ms. {reason}",
            confidence=1.0,
            status=status
        )
