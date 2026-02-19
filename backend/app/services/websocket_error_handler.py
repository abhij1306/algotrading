"""
WebSocket Error Handler - Graceful error handling for WebSocket connections

This module provides decorators and utilities for handling WebSocket errors
gracefully without crashing the application. It ensures that normal disconnections
are logged at INFO level while actual errors are logged at ERROR level with context.

Requirements: 8.1, 8.2
"""
import logging
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from fastapi import WebSocketDisconnect

P = ParamSpec('P')
T = TypeVar('T')

logger = logging.getLogger(__name__)


def handle_websocket_errors(
    log_level: str = "INFO"
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """
    Decorator to handle WebSocket errors gracefully.

    This decorator catches WebSocketDisconnect exceptions (which are normal)
    and logs them at the specified level. Other exceptions are logged at ERROR
    level with full context and re-raised.

    Args:
        log_level: Logging level for normal disconnects ("INFO", "DEBUG", etc.)

    Returns:
        Decorated function that handles WebSocket errors gracefully

    Example:
        @handle_websocket_errors(log_level="INFO")
        async def websocket_endpoint(websocket: WebSocket):
            await websocket.accept()
            # ... handle messages ...
    """
    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            try:
                return await func(*args, **kwargs)
            except WebSocketDisconnect:
                # Normal disconnect - log at specified level
                logger.log(
                    getattr(logging, log_level.upper()),
                    f"WebSocket disconnected in {func.__name__}"
                )
                raise  # Re-raise for proper cleanup
            except Exception as e:
                # Actual error - log at ERROR with context
                logger.error(
                    f"Error in {func.__name__}: {e}",
                    exc_info=True
                )
                raise
        return wrapper
    return decorator


def log_websocket_event(event: str, level: str = "INFO", **context):
    """
    Log a WebSocket event with consistent formatting.

    Args:
        event: Event description (e.g., "connection_established", "disconnect")
        level: Logging level ("INFO", "DEBUG", "ERROR", etc.)
        **context: Additional context to include in the log message

    Example:
        log_websocket_event("connection_established", level="INFO", client_id="abc123")
        log_websocket_event("reconnection_attempt", level="DEBUG", attempt=3, max_attempts=10)
    """
    context_str = ", ".join(f"{k}={v}" for k, v in context.items())
    message = f"[WS] {event}"
    if context_str:
        message += f" ({context_str})"

    logger.log(getattr(logging, level.upper()), message)
