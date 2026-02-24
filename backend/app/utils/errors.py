"""
Error Handling Utilities
Sanitizes error messages to prevent information leakage
"""

import logging
import os

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

# Environment check
IS_PRODUCTION = os.getenv("ENVIRONMENT", "development") == "production"


def sanitize_error_message(error: Exception, user_message: str = "An error occurred") -> str:
    """
    Sanitize error message for client response.
    In production, returns generic message. In development, returns detailed error.

    Args:
        error: The exception that occurred
        user_message: Generic message to show to user

    Returns:
        Sanitized error message
    """
    if IS_PRODUCTION:
        # Production: Return generic message only
        return user_message
    else:
        # Development: Include error details for debugging
        return f"{user_message}: {str(error)}"


def handle_api_error(
    error: Exception,
    user_message: str = "An error occurred",
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
    log_level: str = "error",
) -> HTTPException:
    """
    Handle API errors with proper logging and sanitization.

    Args:
        error: The exception that occurred
        user_message: Generic message to show to user
        status_code: HTTP status code
        log_level: Logging level (error, warning, info)

    Returns:
        HTTPException with sanitized message

    Example:
        try:
            # Some operation
            pass
        except Exception as e:
            raise handle_api_error(e, "Failed to process request")
    """
    # Log the full error with stack trace
    log_func = getattr(logger, log_level, logger.error)
    log_func(f"{user_message}: {str(error)}", exc_info=True)

    # Return sanitized error to client
    detail = sanitize_error_message(error, user_message)

    return HTTPException(status_code=status_code, detail=detail)


def handle_validation_error(error: Exception, field: str | None = None) -> HTTPException:
    """
    Handle validation errors.

    Args:
        error: The validation exception
        field: Optional field name that failed validation

    Returns:
        HTTPException with 400 status
    """
    if field:
        message = f"Invalid value for {field}"
    else:
        message = "Validation failed"

    # Validation errors can include more details even in production
    if IS_PRODUCTION:
        detail = message
    else:
        detail = f"{message}: {str(error)}"

    logger.warning(f"Validation error: {detail}")

    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def handle_not_found_error(resource: str, identifier: str | None = None) -> HTTPException:
    """
    Handle resource not found errors.

    Args:
        resource: Type of resource (e.g., "User", "Order")
        identifier: Optional identifier that wasn't found

    Returns:
        HTTPException with 404 status
    """
    if identifier:
        detail = f"{resource} '{identifier}' not found"
    else:
        detail = f"{resource} not found"

    logger.info(f"Not found: {detail}")

    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def handle_unauthorized_error(message: str = "Authentication required") -> HTTPException:
    """
    Handle authentication errors.

    Args:
        message: Error message

    Returns:
        HTTPException with 401 status
    """
    logger.warning(f"Unauthorized access attempt: {message}")

    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=message,
        headers={"WWW-Authenticate": "Bearer"},
    )


def handle_forbidden_error(message: str = "Access denied") -> HTTPException:
    """
    Handle authorization errors.

    Args:
        message: Error message

    Returns:
        HTTPException with 403 status
    """
    logger.warning(f"Forbidden access attempt: {message}")

    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=message)


class SafeHTTPException(HTTPException):
    """
    HTTPException that automatically sanitizes error messages in production.
    """

    def __init__(self, status_code: int, detail: str, user_message: str | None = None, **kwargs):
        """
        Args:
            status_code: HTTP status code
            detail: Detailed error message (logged but not sent in production)
            user_message: Generic message for users (used in production)
        """
        # Log the detailed error
        logger.error(f"HTTP {status_code}: {detail}")

        # Use sanitized message for response
        if IS_PRODUCTION and user_message:
            response_detail = user_message
        else:
            response_detail = detail

        super().__init__(status_code=status_code, detail=response_detail, **kwargs)
