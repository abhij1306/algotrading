"""One HAT runtime per database (DB audit H5).

A second runtime on the same database would reap the first one's running jobs as interrupted,
claim them again and run a second scheduler, fill sync and exit loop on the same accounts. So the
runtime takes a session-level advisory lock (`pg_try_advisory_lock`) on a connection of its own
before it touches anything, holds it for the life of the process and refuses to start when another
process holds it. The lock ends with the connection: a crashed process frees it. The connection
runs in autocommit, so it is never idle in a transaction, and carries `application_name`
`hat_runtime_lock`, so an operator can find its holder (`docs/runbook.md`). The runtime checks the
connection every minute (`alive`): a dead one has lost the lock.
"""

from __future__ import annotations

import logging

from sqlalchemy import Connection, Engine, text

from ..errors import HatConfigError

__all__ = ["RUNTIME_LOCK_APP", "RUNTIME_LOCK_KEY", "RuntimeLock", "RuntimeLockHeldError"]

logger = logging.getLogger(__name__)

RUNTIME_LOCK_KEY = "hat_runtime"
RUNTIME_LOCK_APP = "hat_runtime_lock"
_TRY_LOCK = "SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"


class RuntimeLockHeldError(HatConfigError):
    """Another HAT runtime holds the runtime lock on this database."""


class RuntimeLock:
    """The held lock; `release` closes its connection, which frees it."""

    def __init__(self, connection: Connection) -> None:
        self._connection: Connection | None = connection

    @classmethod
    def acquire(cls, engine: Engine) -> RuntimeLock:
        """Take the lock, or raise `RuntimeLockHeldError` when another process holds it."""
        connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            connection.exec_driver_sql(f"SET application_name = '{RUNTIME_LOCK_APP}'")
            held = connection.execute(text(_TRY_LOCK), {"key": RUNTIME_LOCK_KEY}).scalar_one()
        except BaseException:
            connection.invalidate()
            raise
        if not held:
            connection.invalidate()
            raise RuntimeLockHeldError(
                "another HAT runtime holds the runtime lock on this database "
                f"({RUNTIME_LOCK_APP}); refusing to start"
            )
        logger.info("HAT runtime lock taken")
        return cls(connection)

    def alive(self) -> bool:
        """The lock's connection still answers, so the session, and with it the lock, lives. A
        Postgres restart or a terminated backend ends both: False."""
        connection = self._connection
        if connection is None:
            return False
        try:
            connection.exec_driver_sql("SELECT 1").scalar_one()
        except Exception:
            return False
        return True

    def release(self) -> None:
        """Close the lock's connection (never back to the pool, where the lock would outlive
        the runtime). Safe to call twice."""
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.invalidate()
            connection.close()
