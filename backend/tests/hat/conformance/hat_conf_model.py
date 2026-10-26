"""Model-side helpers for the conformance checks: classify a column, list unique sets."""

import sqlalchemy as sa

import app.hat.models  # noqa: F401  (registers the tables)
from app.base import Base, StateBase


def hat_tables() -> dict[str, sa.Table]:
    tables = {**Base.metadata.tables, **StateBase.metadata.tables}
    return {n: t for n, t in tables.items() if n.startswith("hat_")}


def logical_type(column: sa.Column) -> str:
    """The spec's logical type name of a model column."""
    ty = column.type
    if isinstance(ty, sa.Uuid):
        return "uuid"
    if isinstance(ty, sa.JSON):
        return "jsonb"
    if isinstance(ty, sa.DateTime):
        return "timestamptz" if ty.timezone else "timestamp"
    if isinstance(ty, sa.Numeric):
        return "numeric(18,4)" if (ty.precision, ty.scale) == (18, 4) else "numeric"
    if isinstance(ty, sa.Boolean):
        return "boolean"
    if isinstance(ty, sa.BigInteger):
        return "bigint"
    if isinstance(ty, sa.Integer):
        return "int"
    if isinstance(ty, sa.Date):
        return "date"
    if isinstance(ty, sa.Time):
        return "time"
    if isinstance(ty, sa.CHAR):
        return f"char({ty.length})"
    if isinstance(ty, sa.String):
        return "text"
    return repr(ty)
