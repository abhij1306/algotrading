"""
Custom exceptions for SmartTrader 3.0
Provides structured error responses across the API
"""

from typing import Any

from fastapi import status


class SmartTraderError(Exception):
    """Base exception for all SmartTrader errors"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Convert exception to structured response"""
        response = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            response["error"]["details"] = self.details
        return response


SmartTraderException = SmartTraderError


class DataNotFoundError(SmartTraderError):
    """Raised when requested data doesn't exist"""

    def __init__(
        self, message: str, resource_type: str | None = None, resource_id: str | None = None
    ) -> None:
        details = {}
        if resource_type:
            details["resource_type"] = resource_type
        if resource_id:
            details["resource_id"] = resource_id

        super().__init__(
            code="DATA_NOT_FOUND",
            message=message,
            status_code=status.HTTP_404_NOT_FOUND,
            details=details,
        )


class ValidationError(SmartTraderError):
    """Raised when request validation fails"""

    def __init__(self, message: str, field: str | None = None, value: object | None = None) -> None:
        details = {}
        if field:
            details["field"] = field
        if value is not None:
            details["value"] = str(value)

        super().__init__(
            code="VALIDATION_ERROR",
            message=message,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            details=details,
        )


class InsufficientDataError(SmartTraderError):
    """Raised when not enough data for operation"""

    def __init__(
        self, message: str, required: int | None = None, available: int | None = None
    ) -> None:
        details = {}
        if required is not None:
            details["required"] = required
        if available is not None:
            details["available"] = available

        super().__init__(
            code="INSUFFICIENT_DATA",
            message=message,
            status_code=status.HTTP_400_BAD_REQUEST,
            details=details,
        )


class ExternalAPIError(SmartTraderError):
    """Raised when external API (Fyers, NSE) fails"""

    def __init__(
        self, message: str, service: str | None = None, fallback_available: bool = False
    ) -> None:
        details = {}
        if service:
            details["service"] = service
        details["fallback_available"] = fallback_available

        super().__init__(
            code="EXTERNAL_API_ERROR",
            message=message,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            details=details,
        )


class DatabaseError(SmartTraderError):
    """Raised when database operation fails"""

    def __init__(self, message: str, operation: str | None = None) -> None:
        details = {}
        if operation:
            details["operation"] = operation

        super().__init__(
            code="DATABASE_ERROR",
            message=message,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details=details,
        )


class BacktestError(SmartTraderError):
    """Raised when backtest execution fails"""

    def __init__(self, message: str, strategy_id: str | None = None) -> None:
        details = {}
        if strategy_id:
            details["strategy_id"] = strategy_id

        super().__init__(
            code="BACKTEST_ERROR",
            message=message,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details=details,
        )


class LifecycleTransitionError(SmartTraderError):
    """Raised when strategy lifecycle transition is invalid"""

    def __init__(
        self, message: str, current_state: str | None = None, target_state: str | None = None
    ) -> None:
        details = {}
        if current_state:
            details["current_state"] = current_state
        if target_state:
            details["target_state"] = target_state

        super().__init__(
            code="LIFECYCLE_TRANSITION_ERROR",
            message=message,
            status_code=status.HTTP_400_BAD_REQUEST,
            details=details,
        )


class RateLimitError(SmartTraderError):
    """Raised when rate limit is exceeded"""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        details = {}
        if retry_after:
            details["retry_after_seconds"] = retry_after

        super().__init__(
            code="RATE_LIMIT_EXCEEDED",
            message=message,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            details=details,
        )
