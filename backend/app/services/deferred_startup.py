"""Finish database startup work that was skipped because the database was down.

When the server starts with Postgres stopped, the lifespan runs every step that does not need
the database (Fyers token check, index universe, live market connect) and hands the database
step to a DeferredStartup. Its background task retries the work with backoff (5s doubling to
60s by default) until the database answers, then logs one line and stops. Shutdown cancels it.

A background task was chosen over completing lazily on the first successful request: the work
(create_all plus the ensure_*_schema migrations) must finish before routes rely on the tables,
and a request-path hook would put a multi-second DDL run inside whichever request came first.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum

from ..exceptions import DatabaseUnavailableError
from ..utils.db_errors import is_db_unavailable

logger = logging.getLogger(__name__)

RECOVERED_MESSAGE = "[OK] Database reachable again; deferred startup completed"


class DeferredStartupStatus(StrEnum):
    """What `GET /api/system/health` and `GET /api/websocket/status` report as `startup`."""

    COMPLETE = "COMPLETE"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"


class DeferredStartup:
    """Runs `work` in a worker thread until it stops raising a database-unavailable error.

    `work` raises DatabaseUnavailableError (or any error `is_db_unavailable` recognises) while
    the database is down. Any other error is logged once and ends the retries with FAILED,
    the same outcome a non-DB failure has during normal startup.
    """

    def __init__(
        self,
        work: Callable[[], None],
        *,
        initial_delay: float = 5.0,
        max_delay: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._work = work
        self._initial_delay = initial_delay
        self._max_delay = max_delay
        self._sleep = sleep
        self.status = DeferredStartupStatus.COMPLETE
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Mark the work deferred and start retrying it in the background (once)."""
        if self.task is not None:
            return
        self.status = DeferredStartupStatus.DEFERRED
        self.task = asyncio.get_running_loop().create_task(
            self._run(), name="deferred-database-startup"
        )

    async def stop(self) -> None:
        """Cancel the retry task, if still running, and wait for it to finish."""
        task = self.task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        delay = self._initial_delay
        while True:
            await self._sleep(delay)
            try:
                await asyncio.to_thread(self._work)
            except Exception as exc:
                if not (isinstance(exc, DatabaseUnavailableError) or is_db_unavailable(exc)):
                    self.status = DeferredStartupStatus.FAILED
                    logger.error(
                        "[ERROR] Deferred startup failed after the database came back: %s",
                        exc,
                        exc_info=exc,
                    )
                    return
                delay = min(delay * 2, self._max_delay)
                logger.debug("Database still unavailable; retrying deferred startup in %ss", delay)
                continue
            self.status = DeferredStartupStatus.COMPLETE
            logger.info(RECOVERED_MESSAGE)
            return


def startup_status(app_state: object) -> DeferredStartupStatus:
    """The deferred-startup status kept on `app.state` (COMPLETE when the lifespan did not run)."""
    deferred = getattr(app_state, "deferred_startup", None)
    if isinstance(deferred, DeferredStartup):
        return deferred.status
    return DeferredStartupStatus.COMPLETE
