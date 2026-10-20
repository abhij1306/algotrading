"""Custom (user-defined) universes: create one, read its members back.

Rows live in ``custom_universes`` and ``custom_universe_members``. Both functions take the
caller's session; ``create_custom_universe`` flushes and leaves the commit to the caller.
"""

import logging

from sqlalchemy.orm import Session

from ...models.universe import CustomUniverse, CustomUniverseMember

logger = logging.getLogger(__name__)


class DuplicateCustomUniverseError(ValueError):
    """A custom universe with this code already exists."""

    def __init__(self, universe_code: str, existing_id: int) -> None:
        super().__init__(
            f"Custom universe with code '{universe_code}' already exists (ID: {existing_id})"
        )
        self.universe_code = universe_code
        self.existing_id = existing_id


def create_custom_universe(
    db: Session,
    universe_code: str,
    universe_name: str,
    symbols: list[str],
    description: str = "",
    created_by: str = "system",
) -> CustomUniverse:
    """Add a custom universe and its members to ``db`` and flush; the caller commits.

    Raises ``DuplicateCustomUniverseError`` when ``universe_code`` is already taken.
    """
    existing = (
        db.query(CustomUniverse).filter(CustomUniverse.universe_code == universe_code).first()
    )
    if existing:
        raise DuplicateCustomUniverseError(universe_code, existing.id)

    custom = CustomUniverse(
        universe_code=universe_code,
        universe_name=universe_name,
        description=description,
        created_by=created_by,
    )
    db.add(custom)
    db.flush()

    for symbol in symbols:
        db.add(CustomUniverseMember(universe_id=custom.id, symbol=symbol))

    db.flush()
    logger.info("Created custom universe %s with %d symbols", universe_code, len(symbols))
    return custom


def get_custom_universe_symbols(db: Session, universe_code: str) -> list[str] | None:
    """Return the member symbols of ``universe_code``, or ``None`` if no such universe exists."""
    custom = db.query(CustomUniverse).filter(CustomUniverse.universe_code == universe_code).first()
    if custom is None:
        return None

    members = (
        db.query(CustomUniverseMember).filter(CustomUniverseMember.universe_id == custom.id).all()
    )
    return [m.symbol for m in members]


__all__ = [
    "DuplicateCustomUniverseError",
    "create_custom_universe",
    "get_custom_universe_symbols",
]
