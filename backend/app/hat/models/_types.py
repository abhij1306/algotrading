"""Portable column types and helpers shared by the HAT models."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import CHAR, JSON, Column, DateTime, ForeignKey, Numeric, Uuid
from sqlalchemy.dialects.postgresql import JSONB

from ..ids import uuid7
from ..time import utc_now

# jsonb on Postgres, JSON (text) on SQLite.
JsonB = JSON().with_variant(JSONB(), "postgresql")
# Money: numeric(18, 4). Ratios and weights use the unscaled `Ratio` (numeric), never float.
Money = Numeric(18, 4)
Ratio = Numeric()
# timestamptz; defaults are aware UTC through `now_utc`.
Timestamptz = DateTime(timezone=True)
Currency = CHAR(3)


def now_utc() -> datetime:
    return utc_now()


def uuid_pk() -> Column[uuid.UUID]:
    """`id uuid primary key`, a v7 generated in Python."""
    return Column(Uuid, primary_key=True, default=uuid7)


def fk(target: str, *, nullable: bool = False, primary_key: bool = False) -> Column[uuid.UUID]:
    """A uuid column referencing `target` ("hat_table.column")."""
    return Column(Uuid, ForeignKey(target), nullable=nullable, primary_key=primary_key)


UNIVERSE_VERSION_STATUSES = ("draft", "backtested", "shadow", "live", "retired")


def in_values(column: str, values: Iterable[str]) -> str:
    """SQL text of `column IN (...)` for a plain CHECK constraint both dialects accept (a
    `StrEnum` member is written as its value)."""
    return f"{column} IN ({', '.join(repr(str(v)) for v in values)})"
