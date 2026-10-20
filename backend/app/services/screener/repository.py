"""SQL reads for the screener: each active company's latest bar and the close before it."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, desc, func
from sqlalchemy.orm import Query, Session, aliased

from ...models import Company, HistoricalPrice
from .service import ScreenerCriteria, ScreenerRow, SortSpec

_SORT_COLUMNS: dict[str, Any] = {
    "symbol": Company.symbol,
    "marketcap": Company.market_cap,
    "rsi": HistoricalPrice.rsi_14,
    "macd": HistoricalPrice.macd,
    "adx": HistoricalPrice.adx,
}


class SqlScreenerRepository:
    """Reads `companies` and `historical_prices` through one request-scoped session."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def count(self, criteria: ScreenerCriteria) -> int:
        return self._query(criteria).order_by(None).count()

    def fetch(
        self,
        criteria: ScreenerCriteria,
        order: SortSpec | None = None,
        offset: int | None = None,
        limit: int | None = None,
    ) -> list[ScreenerRow]:
        query = self._query(criteria)
        if order is not None:
            column = _SORT_COLUMNS[order.field]
            primary = desc(column) if order.descending else column
            secondary = (
                desc(Company.symbol)
                if order.descending and order.field == "symbol"
                else Company.symbol.asc()
            )
            query = query.order_by(None).order_by(primary, secondary)
        if offset is not None:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        return [_to_row(company, hist, previous_close) for company, hist, previous_close in query]

    def _query(self, criteria: ScreenerCriteria) -> Query:
        db = self._db
        prev_hist = aliased(HistoricalPrice)
        latest_prices = (
            db.query(
                HistoricalPrice.company_id,
                func.max(HistoricalPrice.date).label("latest_date"),
            )
            .group_by(HistoricalPrice.company_id)
            .subquery()
        )
        previous_prices = (
            db.query(
                HistoricalPrice.company_id.label("company_id"),
                latest_prices.c.latest_date.label("latest_date"),
                func.max(HistoricalPrice.date).label("previous_date"),
            )
            .join(latest_prices, HistoricalPrice.company_id == latest_prices.c.company_id)
            .filter(HistoricalPrice.date < latest_prices.c.latest_date)
            .group_by(HistoricalPrice.company_id, latest_prices.c.latest_date)
            .subquery()
        )

        query = (
            db.query(Company, HistoricalPrice, prev_hist.close.label("previous_close"))
            .join(latest_prices, Company.id == latest_prices.c.company_id)
            .join(
                HistoricalPrice,
                and_(
                    HistoricalPrice.company_id == Company.id,
                    HistoricalPrice.date == latest_prices.c.latest_date,
                ),
            )
            .outerjoin(
                previous_prices,
                and_(
                    previous_prices.c.company_id == Company.id,
                    previous_prices.c.latest_date == latest_prices.c.latest_date,
                ),
            )
            .outerjoin(
                prev_hist,
                and_(
                    prev_hist.company_id == Company.id,
                    prev_hist.date == previous_prices.c.previous_date,
                ),
            )
            .filter(Company.is_active.is_(True))
        )
        if criteria.symbols is not None:
            query = query.filter(Company.symbol.in_(criteria.symbols))
        if criteria.text:
            query = query.filter(
                Company.symbol.ilike(f"%{criteria.text}%")
                | Company.name.ilike(f"%{criteria.text}%")
            )
        return query


def _to_row(company: Company, hist: HistoricalPrice, previous_close: float | None) -> ScreenerRow:
    return ScreenerRow(
        symbol=company.symbol,
        name=company.name,
        sector=company.sector,
        market_cap=company.market_cap,
        close=hist.close,
        volume=hist.volume,
        rsi_14=hist.rsi_14,
        macd=hist.macd,
        adx=hist.adx,
        previous_close=previous_close,
    )
