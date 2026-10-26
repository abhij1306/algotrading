"""Which database a table lives in, and the session that routes each statement to it
(docs/architecture.md, Storage).

hat_live holds the stock data (`app.base.Base`), hat_state the trading and runtime state
(`app.base.StateBase`); the config records are JSON files (`app.hat.configs`). One `HatSession`
binds both metadatas, each to its own engine, so code reads and writes both in one session; a
statement naming tables of both databases has no database to run on and is refused
(`CrossDatabaseError`), never sent. Every connection to hat_live is read-only
(`default_transaction_read_only`) except the data pipeline's and the importers'
(`live_connect_options`), so a stray write to it fails loudly.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import Table
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Mapper, Session
from sqlalchemy.sql import ClauseElement
from sqlalchemy.sql.util import find_tables

from app.base import Base, StateBase

from .errors import HatError

__all__ = [
    "LIVE",
    "STATE",
    "CrossDatabaseError",
    "HatSession",
    "database_of",
    "live_connect_options",
    "session_binds",
    "statement_database",
]

LIVE = "hat_live"
STATE = "hat_state"
READ_ONLY_OPTION = "-c default_transaction_read_only=on"


class CrossDatabaseError(HatError):
    """One statement named tables of both databases (or of none HAT knows)."""


def database_of(table: Table) -> str:
    """The database `table` lives in, by the metadata it is declared on."""
    if table.metadata is Base.metadata:
        return LIVE
    if table.metadata is StateBase.metadata:
        return STATE
    raise CrossDatabaseError(f"table_without_database: {table.name}")


def statement_database(tables: Iterable[Table | None]) -> str | None:
    """The one database every table of a statement lives in (None when it names none); raises
    `CrossDatabaseError` when they span both. What is not a declared table (a function's
    column such as `count(*)`, an ad hoc `table()`) names no database."""
    tables = [t for t in tables if isinstance(t, Table)]
    found = {database_of(t) for t in tables}
    if len(found) > 1:
        names = ", ".join(sorted({t.name for t in tables}))
        raise CrossDatabaseError(f"cross_database_statement: {names}")
    return next(iter(found), None)


def live_connect_options(*, writer: bool) -> str:
    """The libpq `options` of a hat_live connection: read-only by default for every
    transaction, unless the connection is the data pipeline's or an importer's (`writer`)."""
    return "" if writer else READ_ONLY_OPTION


class HatSession(Session):
    """A session over both databases: each statement goes to the engine its tables' metadata is
    bound to, and one naming both is refused before it is sent."""

    def get_bind(
        self,
        mapper: Mapper[Any] | type | None = None,
        *,
        clause: ClauseElement | None = None,
        **kw: Any,  # noqa: ANN401 - Session.get_bind's own keywords
    ) -> Engine | Connection:
        if clause is not None:
            statement_database(find_tables(clause, check_columns=True, include_crud=True))
        return super().get_bind(mapper, clause=clause, **kw)


def session_binds(live: Engine | Connection, state: Engine | Connection) -> dict[Any, Any]:
    """The `binds` of a `HatSession`: each declarative base and each of its tables (so a Core
    statement with no mapper finds its engine too) to its database's engine."""
    binds: dict[Any, Any] = {Base: live, StateBase: state}
    binds.update(dict.fromkeys(Base.metadata.tables.values(), live))
    binds.update(dict.fromkeys(StateBase.metadata.tables.values(), state))
    return binds
