"""Recognise "the database is unreachable" among SQLAlchemy errors.

A down or restarting Postgres surfaces as OperationalError (connection refused,
server closed the connection, timeout), InterfaceError (connection already closed)
or any DBAPIError SQLAlchemy flags with connection_invalidated. Genuine SQL bugs
(ProgrammingError, IntegrityError, DataError) are not matched, so they still
surface as server errors.
"""

import sqlite3

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

# SQLite (tests only) reports schema and syntax bugs as OperationalError; those are
# bugs, not an unreachable database.
_SQLITE_BUG_MARKERS = ("no such ", "syntax error", "has no column")


def _is_unavailable_error(exc: DBAPIError) -> bool:
    if exc.connection_invalidated:
        return True
    if isinstance(exc, InterfaceError):
        return True
    if isinstance(exc, OperationalError):
        if isinstance(exc.orig, sqlite3.OperationalError):
            text = str(exc.orig).lower()
            return not any(marker in text for marker in _SQLITE_BUG_MARKERS)
        return True
    return False


def find_db_unavailable_error(exc: BaseException | None) -> DBAPIError | None:
    """The connection-loss error in exc or its cause/context chain, else None."""
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, DBAPIError) and _is_unavailable_error(current):
            return current
        current = current.__cause__ or current.__context__
    return None


def is_db_unavailable(exc: BaseException | None) -> bool:
    """True when exc, or an exception it was raised from, means the DB is unreachable."""
    return find_db_unavailable_error(exc) is not None


def db_error_summary(exc: DBAPIError) -> str:
    """First line of the driver error: enough to diagnose, never the connection URL."""
    detail = str(exc.orig) if exc.orig is not None else type(exc).__name__
    first_line = detail.strip().splitlines()[0] if detail.strip() else type(exc).__name__
    return f"{type(exc).__name__}: {first_line}"
