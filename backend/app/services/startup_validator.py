"""
Startup Sequence Validator - Ensures correct initialization order

This module provides a structured way to execute and validate the application
startup sequence. It tracks completed steps, handles failures gracefully, and
prevents subsequent steps from executing after a failure.

Requirements: 6.1, 6.4
"""

import logging
from collections.abc import Awaitable, Callable
from enum import Enum

logger = logging.getLogger(__name__)


class StartupStep(Enum):
    """Enumeration of startup steps in the correct execution order."""

    SET_EVENT_LOOP = "set_event_loop"
    VALIDATE_SYMBOL_MASTER = "validate_symbol_master"
    VALIDATE_DATABASE = "validate_database"
    VALIDATE_FYERS_TOKEN = "validate_fyers_token"
    LOAD_INDEX_UNIVERSE = "load_index_universe"
    CONNECT_LIVE_MARKET = "connect_live_market"


class StartupSequence:
    """
    Manages the application startup sequence with validation and error tracking.

    This class ensures that startup steps execute in the correct order and that
    failures are properly logged and handled. If a required step fails, subsequent
    steps are prevented from executing.

    Example:
        sequence = StartupSequence()

        async def setup_database():
            # Database initialization logic
            pass

        success = await sequence.execute_step(
            StartupStep.VALIDATE_DATABASE,
            setup_database,
            required=True
        )

        if not success:
            # Handle failure
            pass
    """

    def __init__(self):
        """Initialize the startup sequence tracker."""
        self.completed_steps: list[StartupStep] = []
        self.failed_step: StartupStep | None = None
        self._has_failure = False

    async def execute_step(
        self, step: StartupStep, func: Callable[[], Awaitable[None]], required: bool = True
    ) -> bool:
        """
        Execute a startup step and track its completion.

        Args:
            step: The startup step to execute
            func: Async function to execute for this step
            required: Whether this step is required for startup to continue

        Returns:
            True if the step completed successfully, False otherwise

        Raises:
            Exception: Re-raises the exception if the step is required and fails
        """
        # Prevent execution if a previous required step failed
        if self._has_failure:
            logger.warning(
                f"Skipping {step.value}: Previous required step failed ({self.failed_step.value})"
            )
            return False

        try:
            logger.info(f"[Startup] Starting: {step.value}")
            await func()
            self.completed_steps.append(step)
            logger.info(f"[Startup] Completed: {step.value}")
            return True

        except Exception as e:
            self.failed_step = step
            logger.error(f"[Startup] Failed: {step.value} - {e}", exc_info=True)

            if required:
                self._has_failure = True
                logger.error(
                    f"[Startup] Required step {step.value} failed. "
                    "Subsequent steps will be skipped."
                )
                raise
            else:
                logger.warning(
                    f"[Startup] Optional step {step.value} failed. Continuing with remaining steps."
                )
                return False

    def is_complete(self) -> bool:
        """
        Check if all required startup steps have completed successfully.

        Returns:
            True if no failures occurred, False otherwise
        """
        return not self._has_failure

    def get_status(self) -> dict:
        """
        Get the current status of the startup sequence.

        Returns:
            Dictionary containing completed steps, failed step, and overall status
        """
        return {
            "completed_steps": [step.value for step in self.completed_steps],
            "failed_step": self.failed_step.value if self.failed_step else None,
            "is_complete": self.is_complete(),
            "total_completed": len(self.completed_steps),
        }

    def log_summary(self):
        """Log a summary of the startup sequence execution."""
        if self.is_complete():
            logger.info(
                f"[Startup] All steps completed successfully. "
                f"Total: {len(self.completed_steps)} steps"
            )
        else:
            logger.error(
                f"[Startup] Startup sequence incomplete. "
                f"Failed at: {self.failed_step.value if self.failed_step else 'unknown'}, "
                f"Completed: {len(self.completed_steps)} steps"
            )
