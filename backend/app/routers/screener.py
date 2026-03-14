import logging
import threading
import time
from collections import OrderedDict
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import and_, desc, func
from sqlalchemy.orm import Session

from ..constants.indices import DEFAULT_SCREENER_UNIVERSE
from ..data_fetcher import fetch_fyers_quotes
from ..database import SessionLocal
from ..models import Company, HistoricalPrice
from ..services.index_universe_loader import index_universe_loader

router = APIRouter()
logger = logging.getLogger(__name__)

_SCREENER_CACHE_TTL_SECONDS = 5.0
_SCREENER_CACHE_MAXSIZE = 256
_screener_cache_lock = threading.Lock()
_screener_cache: OrderedDict[tuple, tuple[float, dict[str, Any]]] = OrderedDict()
RESULTS_ERROR_RESPONSES = {
    400: {"description": "Invalid screener input"},
    500: {"description": "Failed to load screener results"},
}
INDICES_ERROR_RESPONSES = {500: {"description": "Failed to load screener universes"}}


def _get_cached_screener_payload(cache_key: tuple[Any, ...]) -> dict[str, Any] | None:
    now = time.time()
    with _screener_cache_lock:
        expired_keys = [
            key
            for key, (cached_at, _) in _screener_cache.items()
            if (now - cached_at) >= _SCREENER_CACHE_TTL_SECONDS
        ]
        for key in expired_keys:
            _screener_cache.pop(key, None)

        cached = _screener_cache.get(cache_key)
        if cached is None:
            return None

        _screener_cache.move_to_end(cache_key)
        return cached[1]


def _store_screener_payload(cache_key: tuple[Any, ...], payload: dict[str, Any]) -> None:
    with _screener_cache_lock:
        _screener_cache[cache_key] = (time.time(), payload)
        _screener_cache.move_to_end(cache_key)
        while len(_screener_cache) > _SCREENER_CACHE_MAXSIZE:
            _screener_cache.popitem(last=False)


def _normalize_screener_request(
    universe: str,
    query: str | None,
    page: int,
    limit: int,
    full: bool,
    sort_by: str,
    sort_order: str,
) -> tuple[tuple[Any, ...], str, str | None, str, str]:
    normalized_universe = universe.upper()
    normalized_query = query.strip().lower() if query else None
    normalized_sort = (sort_by or "symbol").lower()
    normalized_order = (sort_order or "asc").lower()
    cache_key = (
        normalized_universe,
        normalized_query,
        page,
        limit,
        bool(full),
        normalized_sort,
        normalized_order,
    )
    return cache_key, normalized_universe, normalized_query, normalized_sort, normalized_order


def _build_screener_query(
    db: Session, normalized_universe: str, normalized_query: str | None
) -> object:
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
            return None
        companies_query = companies_query.filter(Company.symbol.in_(index_symbols))

    if normalized_query:
        companies_query = companies_query.filter(
            Company.symbol.ilike(f"%{normalized_query}%")
            | Company.name.ilike(f"%{normalized_query}%")
        )
    return companies_query


def _apply_screener_sort(
    companies_query: object, normalized_order: str, normalized_sort: str
) -> object:
    descending = normalized_order == "desc"
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
    sort_expr = sort_mapping.get(normalized_sort)
    if sort_expr is None:
        raise HTTPException(status_code=400, detail=f"Invalid sort field: {normalized_sort}")
    return companies_query.order_by(None).order_by(desc(sort_expr) if descending else sort_expr)


def _hydrate_quotes(symbols: list[str], full: bool) -> dict[str, dict[str, Any]]:
    if not symbols or full:
        return {}
    try:
        return fetch_fyers_quotes(symbols) or {}
    except Exception as exc:
        logger.warning("Live quote hydration failed: %s", exc)
        return {}


def _serialize_screener_row(
    company: Company, hist: HistoricalPrice, quote: dict[str, Any] | None
) -> dict[str, Any]:
    price = float(hist.close or 0)
    volume = int(hist.volume or 0)
    change = ((hist.close - hist.open) / hist.open * 100) if hist.open else 0.0

    price, volume, change = _apply_quote_to_row(price, volume, change, quote)

    return {
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


def _apply_quote_to_row(
    price: float,
    volume: int,
    change: float,
    quote: dict[str, Any] | None,
) -> tuple[float, int, float]:
    if not quote:
        return price, volume, change

    quote_price = quote.get("ltp")
    quote_volume = quote.get("volume")
    prev_close = quote.get("prev_close")

    if isinstance(quote_price, (int, float)) and quote_price > 0:
        price = float(quote_price)
        if isinstance(prev_close, (int, float)) and prev_close > 0:
            change = ((price - prev_close) / prev_close) * 100

    if isinstance(quote_volume, (int, float)) and quote_volume > 0:
        volume = int(quote_volume)

    return price, volume, change


def _load_screener_payload(
    db: Session,
    normalized_universe: str,
    normalized_query: str | None,
    normalized_sort: str,
    normalized_order: str,
    page: int,
    limit: int,
    full: bool,
) -> dict[str, Any]:
    companies_query = _build_screener_query(db, normalized_universe, normalized_query)
    if companies_query is None:
        return {"results": [], "total": 0, "page": page, "limit": limit}

    companies_query = _apply_screener_sort(companies_query, normalized_order, normalized_sort)
    total_records = companies_query.order_by(None).count()
    if full:
        rows = companies_query.limit(1200).all()
    else:
        rows = companies_query.offset((page - 1) * limit).limit(limit).all()
    live_quotes = _hydrate_quotes([company.symbol for company, _ in rows], full)
    return {
        "results": [
            _serialize_screener_row(company, hist, live_quotes.get(company.symbol))
            for company, hist in rows
        ],
        "total": total_records,
        "page": page,
        "limit": limit,
    }


@router.get("/indices", responses=INDICES_ERROR_RESPONSES)
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


@router.get("/results", responses=RESULTS_ERROR_RESPONSES)
def get_screener_results(
    universe: Annotated[str, Query(min_length=1)] = DEFAULT_SCREENER_UNIVERSE,
    query: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    full: Annotated[bool, Query()] = False,
    sort_by: Annotated[str, Query()] = "symbol",
    sort_order: Annotated[str, Query()] = "asc",
) -> dict[str, Any]:
    """
    Return screener rows using DB technicals with initial live quote hydration.
    Canonical Phase-1 endpoint contract.
    """
    db = None
    try:
        db = SessionLocal()

        (
            cache_key,
            normalized_universe,
            normalized_query,
            normalized_sort,
            normalized_order,
        ) = _normalize_screener_request(
            universe,
            query,
            page,
            limit,
            full,
            sort_by,
            sort_order,
        )

        if not full:
            cached = _get_cached_screener_payload(cache_key)
            if cached is not None:
                return cached

        available_universes = set(index_universe_loader.get_available_indices())

        if normalized_universe != "ALL" and normalized_universe not in available_universes:
            raise HTTPException(status_code=400, detail=f"Unknown universe: {universe}")

        payload = _load_screener_payload(
            db,
            normalized_universe,
            normalized_query,
            normalized_sort,
            normalized_order,
            page,
            limit,
            full,
        )
        if not full:
            _store_screener_payload(cache_key, payload)
        return payload
    finally:
        if db:
            db.close()
