"""
Custom exceptions for SmartTrader 3.0
Provides structured error responses across the API
"""
from fastapi import HTTPException, status
from typing import Optional, Dict, Any


class SmartTraderException(Exception):
    """Base exception for all SmartTrader errors"""
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        details: Optional[Dict[str, Any]] = None
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> Dict[str, Any]:
        """Convert exception to structured response"""
        response = {
            "error": {
                "code": self.code,
                "message": self.message
            }
        }
        if self.details:
            response["error"]["details"] = self.details
        return response


class DataNotFoundError(SmartTraderException):
    """Raised when requested data doesn't exist"""
    def __init__(self, message: str, resource_type: Optional[str] = None, resource_id: Optional[str] = None):
        details = {}
        if resource_type:
            details["resource_type"] = resource_type
        if resource_id:
            details["resource_id"] = resource_id
        
        super().__init__(
            code="DATA_NOT_FOUND",
            message=message,
            status_code=status.HTTP_404_NOT_FOUND,
            details=details
        )


class ValidationError(SmartTraderException):
    """Raised when request validation fails"""
    def __init__(self, message: str, field: Optional[str] = None, value: Optional[Any] = None):
        details = {}
        if field:
            details["field"] = field
        if value is not None:
            details["value"] = str(value)
        
        super().__init__(
            code="VALIDATION_ERROR",
            message=message,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            details=details
        )


class InsufficientDataError(SmartTraderException):
    """Raised when not enough data for operation"""
    def __init__(self, message: str, required: Optional[int] = None, available: Optional[int] = None):
        details = {}
        if required is not None:
            details["required"] = required
        if available is not None:
            details["available"] = available
        
        super().__init__(
            code="INSUFFICIENT_DATA",
            message=message,
            status_code=status.HTTP_400_BAD_REQUEST,
            details=details
        )


class ExternalAPIError(SmartTraderException):
    """Raised when external API (Fyers, NSE) fails"""
    def __init__(self, message: str, service: Optional[str] = None, fallback_available: bool = False):
        details = {}
        if service:
            details["service"] = service
        details["fallback_available"] = fallback_available
        
        super().__init__(
            code="EXTERNAL_API_ERROR",
            message=message,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            details=details
        )


class DatabaseError(SmartTraderException):
    """Raised when database operation fails"""
    def __init__(self, message: str, operation: Optional[str] = None):
        details = {}
        if operation:
            details["operation"] = operation
        
        super().__init__(
            code="DATABASE_ERROR",
            message=message,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details=details
        )


class BacktestError(SmartTraderException):
    """Raised when backtest execution fails"""
    def __init__(self, message: str, strategy_id: Optional[str] = None):
        details = {}
        if strategy_id:
            details["strategy_id"] = strategy_id
        
        super().__init__(
            code="BACKTEST_ERROR",
            message=message,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details=details
        )


class LifecycleTransitionError(SmartTraderException):
    """Raised when strategy lifecycle transition is invalid"""
    def __init__(self, message: str, current_state: Optional[str] = None, target_state: Optional[str] = None):
        details = {}
        if current_state:
            details["current_state"] = current_state
        if target_state:
            details["target_state"] = target_state
        
        super().__init__(
            code="LIFECYCLE_TRANSITION_ERROR",
            message=message,
            status_code=status.HTTP_400_BAD_REQUEST,
            details=details
        )


class RateLimitError(SmartTraderException):
    """Raised when rate limit is exceeded"""
    def __init__(self, message: str, retry_after: Optional[int] = None):
        details = {}
        if retry_after:
            details["retry_after_seconds"] = retry_after
        
        super().__init__(
            code="RATE_LIMIT_EXCEEDED",
            message=message,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            details=details
        )
