import logging

from ..database import AgentAuditLog, SessionLocal

logger = logging.getLogger(__name__)


class AuditLogger:
    """
    Centralized logging for Agent Actions and Broker API calls.
    Persists to AgentAuditLog table.
    """

    @staticmethod
    def log_decision(
        agent_name: str,
        action_type: str,
        symbol: str | None,
        input_snapshot: dict,
        decision: dict,
        reasoning: str,
        confidence: float,
        status: str = "SUCCESS",
    ) -> None:
        """Log a high-level agent decision (e.g., Signal Generated, Trade Executed)"""
        db = SessionLocal()
        try:
            log = AgentAuditLog(
                agent_name=agent_name,
                action_type=action_type,
                symbol=symbol,
                input_snapshot=input_snapshot,  # SQLAlchemy JSON handles dict
                decision=decision,
                reasoning=reasoning,
                confidence=confidence,
                status=status,
                execution_time_ms=0,  # TODO(#8): measure time
            )
            db.add(log)
            db.commit()
        except Exception as e:
            logger.error("[AUDIT FAIL] %s", e)
        finally:
            db.close()

    @staticmethod
    def log_api_call(
        broker_name: str,
        method: str,
        params: dict,
        response: object,
        duration_ms: int,
        error: str = None,
    ) -> None:
        """Log a broker API call (Simulated or Real)"""
        # For API logs, we might want a separate table 'SystemLogs'
        # or use AuditLog with specific types
        # Using AgentAuditLog with type="BROKER_API"

        status = "FAILURE" if error else "SUCCESS"
        reason = error if error else "API Call Success"

        # Sanitize Params (Remove Secrets if any)
        # ...

        AuditLogger.log_decision(
            agent_name=f"BROKER::{broker_name}",
            action_type=f"API::{method}",
            symbol=params.get("symbol"),
            input_snapshot=params,
            decision={"response": str(response)},  # Store string repr if complex
            reasoning=f"Duration: {duration_ms}ms. {reason}",
            confidence=1.0,
            status=status,
        )
