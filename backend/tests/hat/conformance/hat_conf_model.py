"""Model-side helpers for the conformance checks: classify a column, list unique sets."""

import sqlalchemy as sa

import app.hat.models  # noqa: F401  (registers the tables)
from app.base import Base


def hat_tables() -> dict[str, sa.Table]:
    return {n: t for n, t in Base.metadata.tables.items() if n.startswith("hat_")}


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


def unique_sets(table: sa.Table) -> set[frozenset[str]]:
    """Column sets that are unique: PK, unique constraints, unique non-partial indexes."""
    found = {frozenset(c.name for c in table.primary_key.columns)}
    for constraint in table.constraints:
        if isinstance(constraint, sa.UniqueConstraint):
            found.add(frozenset(c.name for c in constraint.columns))
    for index in table.indexes:
        if index.unique and index.dialect_options["postgresql"].get("where") is None:
            found.add(frozenset(c.name for c in index.columns))
    for column in table.columns:
        if column.unique:
            found.add(frozenset({column.name}))
    return found
