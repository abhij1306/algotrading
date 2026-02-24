import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import and_, desc, func

from ..constants.indices import DEFAULT_SCREENER_UNIVERSE
from ..data_fetcher import fetch_fyers_quotes
from ..database import SessionLocal
from ..models import Company, HistoricalPrice
from ..services.index_universe_loader import index_universe_loader

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/indices")
def get_indices(response: Response) -> dict[str, Any]:
    """Return available screener universes from IndexUniverseLoader."""
    response.headers["Cache-Control"] = "public, max-age=3600"

    try:
        available_indices = index_universe_loader.get_available_indices()
        indices: list[dict[str, Any]] = []

        for index_id in available_indices:
            universe = index_universe_loader.get_index_universe(index_id)
            indices.append(
                {
                    "id": index_id,
                    "name": index_universe_loader.get_index_description(index_id) or index_id,
                    "count": len(universe) if universe else 0,
                }
            )

        indices.sort(key=lambda item: item["name"])
        return {
            "indices": indices,
            "default": DEFAULT_SCREENER_UNIVERSE,
            "total_loaded": len(indices),
        }
    except Exception as exc:
        logger.error("Failed to load screener indices: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to load screener universes") from exc


@router.get("/results")
def get_screener_results(
    universe: str = Query(default=DEFAULT_SCREENER_UNIVERSE, min_length=1),
    query: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    full: bool = Query(default=False),
    sort_by: str = Query(default="symbol"),
    sort_order: str = Query(default="asc"),
) -> dict[str, Any]:
    """
    Return screener rows using DB technicals with initial live quote hydration.
    Canonical Phase-1 endpoint contract.
    """
    db = None
    try:
        db = SessionLocal()

        normalized_universe = universe.upper()
        available_universes = set(index_universe_loader.get_available_indices())

        if normalized_universe != "ALL" and normalized_universe not in available_universes:
            raise HTTPException(status_code=400, detail=f"Unknown universe: {universe}")

        latest_prices_subquery = (
            db.query(
                HistoricalPrice.company_id,
                func.max(HistoricalPrice.date).label("latest_date"),
            )
            .group_by(HistoricalPrice.company_id)
            .subquery()
        )

        companies_query = (
            db.query(Company, HistoricalPrice)
            .join(latest_prices_subquery, Company.id == latest_prices_subquery.c.company_id)
            .join(
                HistoricalPrice,
                and_(
                    HistoricalPrice.company_id == Company.id,
                    HistoricalPrice.date == latest_prices_subquery.c.latest_date,
                ),
            )
            .filter(Company.is_active.is_(True))
        )

        if normalized_universe != "ALL":
            index_symbols = index_universe_loader.get_index_symbols(normalized_universe)
            if not index_symbols:
                return {"results": [], "total": 0, "page": page, "limit": limit}
            companies_query = companies_query.filter(Company.symbol.in_(index_symbols))

        if query:
            companies_query = companies_query.filter(
                Company.symbol.ilike(f"%{query}%") | Company.name.ilike(f"%{query}%")
            )

        normalized_sort = (sort_by or "symbol").lower()
        descending = (sort_order or "asc").lower() == "desc"
        change_expr = (
            (HistoricalPrice.close - HistoricalPrice.open) / func.nullif(HistoricalPrice.open, 0)
        ) * 100

        sort_mapping = {
            "symbol": Company.symbol,
            "price": HistoricalPrice.close,
            "change": change_expr,
            "volume": HistoricalPrice.volume,
            "marketcap": Company.market_cap,
            "rsi": HistoricalPrice.rsi_14,
            "macd": HistoricalPrice.macd,
            "adx": HistoricalPrice.adx,
        }

        sort_expr = sort_mapping.get(normalized_sort, Company.symbol)
        companies_query = companies_query.order_by(None).order_by(
            desc(sort_expr) if descending else sort_expr
        )

        total_records = companies_query.count()
        if full:
            # Guardrail: prevent accidental oversized responses.
            rows = companies_query.limit(1200).all()
        else:
            offset = (page - 1) * limit
            rows = companies_query.offset(offset).limit(limit).all()

        symbols = [company.symbol for company, _ in rows]
        live_quotes: dict[str, dict[str, Any]] = {}
        if symbols and not full:
            try:
                live_quotes = fetch_fyers_quotes(symbols) or {}
            except Exception as exc:
                logger.warning("Live quote hydration failed: %s", exc)

        results_list: list[dict[str, Any]] = []
        for company, hist in rows:
            price = float(hist.close or 0)
            volume = int(hist.volume or 0)
            change = ((hist.close - hist.open) / hist.open * 100) if hist.open else 0.0

            quote = live_quotes.get(company.symbol)
            if quote:
                quote_price = quote.get("ltp")
                quote_volume = quote.get("volume")
                prev_close = quote.get("prev_close")

                if isinstance(quote_price, (int, float)) and quote_price > 0:
                    price = float(quote_price)
                    if isinstance(prev_close, (int, float)) and prev_close > 0:
                        change = ((price - prev_close) / prev_close) * 100

                if isinstance(quote_volume, (int, float)) and quote_volume > 0:
                    volume = int(quote_volume)

            results_list.append(
                {
                    "symbol": company.symbol,
                    "name": company.name,
                    "sector": company.sector or "Unknown",
                    "marketCap": float(company.market_cap or 0),
                    "change": round(float(change), 2),
                    "price": round(float(price), 2),
                    "volume": volume,
                    "rsi": round(float(hist.rsi_14 or 0), 2),
                    "macd": round(float(hist.macd or 0), 4),
                    "adx": round(float(hist.adx or 0), 2),
                }
            )

        return {
            "results": results_list,
            "total": total_records,
            "page": page,
            "limit": limit,
        }
    finally:
        if db:
            db.close()
