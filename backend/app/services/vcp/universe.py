"""Point-in-time index membership and company metadata for VCP scans and backtests.

A member on date d has `effective_from <= d` and no `effective_to` before d, so names
that left the index are still scanned on dates they belonged to and names added later are
not (no survivorship bias). The provenance label says how the list was resolved:
`bias_corrected` (point in time), `indicative_pre_<date>` (before the recorded history,
today's members), `live_fallback` (no members on that date, today's members) or
`unavailable` (unknown universe).
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session

from ...models import Company, IndexConstituentHistory, IndexUniverseDefinition


def _definition(db: Session, universe: str) -> IndexUniverseDefinition | None:
    return (
        db.query(IndexUniverseDefinition)
        .filter(IndexUniverseDefinition.index_code == universe.upper().strip())
        .first()
    )


def _current_members(db: Session, universe_id: int) -> list[str]:
    rows = (
        db.query(IndexConstituentHistory.symbol)
        .filter(
            IndexConstituentHistory.universe_id == universe_id,
            IndexConstituentHistory.effective_to.is_(None),
        )
        .order_by(IndexConstituentHistory.symbol.asc())
        .all()
    )
    return [row[0] for row in rows]


def constituents_for_date(db: Session, universe: str, as_of: date) -> tuple[list[str], str]:
    """(members of `universe` on `as_of`, sorted; provenance label)."""
    definition = _definition(db, universe)
    if definition is None:
        return [], "unavailable"

    earliest = (
        db.query(func.min(IndexConstituentHistory.effective_from))
        .filter(IndexConstituentHistory.universe_id == definition.id)
        .scalar()
    )
    if earliest and as_of < earliest:
        return _current_members(db, definition.id), f"indicative_pre_{earliest.isoformat()}"

    rows = (
        db.query(IndexConstituentHistory.symbol)
        .filter(
            IndexConstituentHistory.universe_id == definition.id,
            IndexConstituentHistory.effective_from <= as_of,
            (
                IndexConstituentHistory.effective_to.is_(None)
                | (IndexConstituentHistory.effective_to >= as_of)
            ),
        )
        .order_by(IndexConstituentHistory.symbol.asc())
        .all()
    )
    symbols = [row[0] for row in rows]
    if symbols:
        return symbols, "bias_corrected"
    return _current_members(db, definition.id), "live_fallback"


def constituents_in_range(
    db: Session, universe: str, start_date: date, end_date: date
) -> list[str]:
    """Every symbol that was a member at any point in [start_date, end_date]."""
    definition = _definition(db, universe)
    if definition is None:
        return []
    rows = (
        db.query(IndexConstituentHistory.symbol)
        .filter(
            IndexConstituentHistory.universe_id == definition.id,
            IndexConstituentHistory.effective_from <= end_date,
            (
                IndexConstituentHistory.effective_to.is_(None)
                | (IndexConstituentHistory.effective_to >= start_date)
            ),
        )
        .all()
    )
    return sorted({row[0] for row in rows})


def company_meta(db: Session, symbols: list[str]) -> dict[str, dict[str, str]]:
    """Name and sector per symbol; symbols without a `companies` row are left out."""
    if not symbols:
        return {}
    rows = db.query(Company).filter(Company.symbol.in_(symbols)).all()
    return {
        row.symbol: {"name": row.name or row.symbol, "sector": row.sector or "Unknown"}
        for row in rows
    }
