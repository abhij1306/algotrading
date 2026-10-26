"""The NSE symbol of an instrument, read on its own: instruments live in hat_live, positions and
signals in hat_state, so one statement never joins them (`storage.CrossDatabaseError`)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HatInstrument

__all__ = ["instrument_symbols"]


def instrument_symbols(session: Session, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    """The NSE symbol of each instrument in `ids`: instruments live in hat_live, positions and
    signals in hat_state, so one statement never joins them (`storage.CrossDatabaseError`)."""
    wanted = set(ids)
    if not wanted:
        return {}
    rows = session.execute(
        select(HatInstrument.id, HatInstrument.symbol).where(HatInstrument.id.in_(wanted))
    ).tuples()
    return dict(rows)
