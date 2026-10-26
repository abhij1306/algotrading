"""Helpers shared by the HAT verification suites."""

import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy.pool import StaticPool

from app.base import Base, StateBase
from app.hat.configs.store import MemoryConfigStore
from app.hat.configs.unit import STORE


def memory_engine(*models: type[DeclarativeBase] | type) -> Engine:
    """A fresh in-memory SQLite database holding the tables of `models` (all HAT tables if none).

    A StaticPool keeps one connection, so the database lives as long as the engine and every
    session sees the same data.
    """
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    tables = [m.__table__ for m in models]  # type: ignore[attr-defined]
    for base in (Base, StateBase):
        mine = [t for t in tables if t.metadata is base.metadata]
        if mine or not tables:
            base.metadata.create_all(engine, tables=mine or None)
    return engine


def memory_session(engine: Engine) -> Session:
    """A session on a `memory_engine` with its own in-memory config store (`configs`)."""
    return Session(engine, info={STORE: MemoryConfigStore()})


# Where a command line's HAT_DATA_ROOT comes from: only the `.env` file, only the
# process environment, both (the environment wins), or a blank environment value over a `.env`
# value (blank means the default).
ROOT_SOURCES = ("env_file", "environment", "both", "blank_environment")


@contextmanager
def operator_env(scratch: Path, source: str) -> Iterator[Path]:
    """Run a command line with its `HAT_DATA_ROOT` from `source` (one of ROOT_SOURCES); yields
    the root it must use. The `.env` file, every root and the default all sit in `scratch`, so
    a command that resolves its root wrongly writes to a wrong scratch directory, never to a
    real data root; the process environment is restored afterwards. The database module is
    imported first: its import loads the repository's own `.env`, and done inside the run it
    could put a real `HAT_DATA_ROOT` back after this helper removed it."""
    import app.database  # noqa: F401, PLC0415 - its .env load must happen before the run

    from_file, from_env, default = (
        scratch / "env_file",
        scratch / "environment",
        scratch / "default",
    )
    env_file = scratch / ".env"
    env_file.write_text(
        "" if source == "environment" else f"HAT_DATA_ROOT={from_file}\n", encoding="utf-8"
    )
    wanted = {"env_file": from_file, "blank_environment": default}.get(source, from_env)
    with (
        patch.dict(os.environ),
        patch("app.hat.settings.ENV_FILE", env_file),
        patch("app.hat.settings.HAT_DATA_ROOT", default),
    ):
        os.environ.pop("HAT_DATA_ROOT", None)
        if source in ("environment", "both"):
            os.environ["HAT_DATA_ROOT"] = str(from_env)
        elif source == "blank_environment":
            os.environ["HAT_DATA_ROOT"] = ""
        yield wanted


@contextmanager
def no_network() -> Iterator[list[object]]:
    """Name lookups and socket connects fail and are recorded in the yielded list; a check
    asserts it stays empty, since a fetcher may turn the failure into an ordinary answer."""
    reached: list[object] = []

    def refuse(*args: object, **_kwargs: object) -> None:
        reached.append(args)
        raise OSError("a check reached the network")

    with (
        patch("socket.getaddrinfo", refuse),
        patch.object(socket.socket, "connect", refuse),
        patch("socket.create_connection", refuse),
    ):
        yield reached
