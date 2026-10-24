"""The stored regime states, newest trade date first."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HatRegimeState

__all__ = ["RegimeRow", "latest_regimes"]


@dataclass(frozen=True)
class RegimeRow:
    trade_date: date
    state: str
    confirmed: bool


def latest_regimes(session: Session, limit: int) -> list[RegimeRow]:
    rows = session.scalars(
        select(HatRegimeState).order_by(HatRegimeState.trade_date.desc()).limit(limit)
    )
    return [RegimeRow(r.trade_date, r.state, r.confirmed) for r in rows]
