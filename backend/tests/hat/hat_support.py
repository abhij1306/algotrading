"""Helpers shared by the HAT verification suites."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from app.base import Base


def memory_engine(*models: type[DeclarativeBase] | type) -> Engine:
    """A fresh in-memory SQLite database holding the tables of `models` (all HAT tables if none).

    A StaticPool keeps one connection, so the database lives as long as the engine and every
    session sees the same data.
    """
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    tables = [m.__table__ for m in models] if models else None  # type: ignore[attr-defined]
    Base.metadata.create_all(engine, tables=tables)
    return engine
