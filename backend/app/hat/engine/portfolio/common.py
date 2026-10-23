"""What the portfolio services share: the refusal they raise, the market they act in and the
instant a trade date's portfolio decisions are taken at (its close)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.errors import HatError
from app.hat.models import HatMarket
from app.hat.time import IST

ALLOCATION_AT = time(15, 30)  # the policy version in force at the close of the trade date


class PortfolioError(HatError):
    """A portfolio operation was refused; `code` names the rule and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def close_of(day: date) -> datetime:
    return datetime.combine(day, ALLOCATION_AT, tzinfo=IST)


def market_id_of(session: Session, code: str = "NSE") -> uuid.UUID:
    found = session.scalar(select(HatMarket.id).where(HatMarket.code == code))
    if found is None:
        raise PortfolioError("market_unknown")
    return found
