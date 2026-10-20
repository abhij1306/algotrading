"""
Error Handling Utilities
Sanitizes error messages to prevent information leakage
"""

import logging

from fastapi import HTTPException, status

from .logger import safe_log_value

logger = logging.getLogger(__name__)


def sanitize_error_message(error: Exception, user_message: str = "An error occurred") -> str:
    """
    Client-facing text for an unexpected error: always the fixed user message.

    The exception text (driver, broker or file-system detail) is never sent to the client,
    in any environment; callers log it server-side.

    Args:
        error: The exception that occurred (unused; kept for call-site clarity)
        user_message: Fixed message to show to the user

    Returns:
        The user message
    """
    del error
    return user_message


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
    log_func("%s: %s", user_message, safe_log_value(error), exc_info=True)

    # Return sanitized error to client
    detail = sanitize_error_message(error, user_message)

    return HTTPException(status_code=status_code, detail=detail)


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
