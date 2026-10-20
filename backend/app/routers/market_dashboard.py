import threading
import time
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Company, HistoricalPrice
from ..models.universe import IndexConstituentHistory, IndexUniverseDefinition
from ..services.live_market_service import live_market
from ..services.market_data_service import MarketDataService
from ..services.yfinance_service import yfinance_service

router = APIRouter(prefix="/market", tags=["Market Dashboard"])
DB_DEPENDENCY = Depends(get_db)

_overview_cache_lock = threading.Lock()
_overview_cache_ts: float = 0.0
_overview_cache_value: dict | None = None

_top_gainers_cache_lock = threading.Lock()
_top_gainers_cache: dict[tuple[int, str], tuple[float, dict]] = {}


@router.get("/overview", response_model=None)
def get_market_overview() -> dict[str, Any]:
    """
    Returns a comprehensive market overview including:
    - Global Indices (US, India, etc.)
    - Commodities (Gold, Silver)
    - Market Sentiment (Fear & Greed, India VIX)
    - Technical Market Condition (Trend/Range)
    """
    global _overview_cache_ts, _overview_cache_value
    now = time.time()
    with _overview_cache_lock:
        if _overview_cache_value is not None and (now - _overview_cache_ts) < 15:
            return _overview_cache_value
    try:
        indices = MarketDataService.get_global_indices()
        sentiment = MarketDataService.get_market_sentiment()
        condition = MarketDataService.get_market_condition()

        payload = {
            "indices": indices,
            "sentiment": sentiment,
            "condition": condition,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    except Exception as e:
        payload = {"error": str(e), "timestamp": datetime.now(UTC).isoformat()}

    with _overview_cache_lock:
        _overview_cache_ts = now
        _overview_cache_value = payload
    return payload


@router.get("/sentiment", response_model=None)
def get_market_sentiment_endpoint(db: Session = DB_DEPENDENCY) -> dict[str, Any]:
    """
    Legacy/Specific sentiment endpoint if needed, or mapped to new service.
    """
    return MarketDataService.get_market_sentiment()


@router.get("/indices", response_model=None)
def get_index_performance() -> list[dict[str, Any]]:
    """
    Legacy/Specific indices endpoint.
    """
    return MarketDataService.get_global_indices()


@router.get("/market-overview", response_model=None)
async def get_market_overview_v2(db: Session = DB_DEPENDENCY) -> dict[str, Any]:
    """
    Get market overview with live/delayed data
    During market hours: Live data from Fyers
    Post-market: Delayed data from yfinance
    """
    # Key indices to track
    indices = ["NIFTY50", "BANKNIFTY"]

    # Check if market is open
    is_market_open = live_market.is_market_open()

    data = {
        "market_status": await live_market.get_status(),
        "is_live": is_market_open,
        "indices": {},
    }

    if is_market_open:
        # Try to get live data from cache
        data["indices"].update(_live_index_quotes(indices))

    # Fallback for indices not found in live cache or if market is closed
    missing_indices = [idx for idx in indices if idx not in data["indices"]]
    if missing_indices:
        # Try yfinance, then the database
        quotes = yfinance_service.get_quotes(missing_indices)
        for index, quote in quotes.items():
            fallback = quote or _database_index_quote(db, index)
            if fallback:
                data["indices"][index] = fallback

    return data


def _live_index_quotes(indices: list[str]) -> dict[str, dict[str, Any]]:
    """Price and change of each index with a cached live tick."""
    quotes = {}
    for index in indices:
        tick = live_market.get_latest_tick(index)
        if tick:
            quotes[index] = {
                "price": tick.get("ltp") or tick.get("price"),
                "change_pct": tick.get("chp") or tick.get("change_pct"),
                "source": "live",
            }
    return quotes


def _database_index_quote(db: Session, index: str) -> dict[str, Any] | None:
    """Latest stored close of an index, or None when there is none."""
    latest_price = (
        db.query(HistoricalPrice)
        .join(Company)
        .filter(Company.symbol == index)
        .order_by(HistoricalPrice.date.desc())
        .first()
    )
    if not latest_price:
        return None
    return {
        "price": float(latest_price.close),
        "change_pct": 0,
        "source": "database",
        "date": latest_price.date.isoformat(),
    }


@router.get("/top-gainers", response_model=None)
def get_top_gainers(
    limit: int = 10,
    index: str = "NIFTY50",
    db: Session = DB_DEPENDENCY,
) -> dict[str, Any]:
    """Get top gainers with live/delayed data"""
    cache_key = (limit, index)
    now = time.time()
    with _top_gainers_cache_lock:
        cached = _top_gainers_cache.get(cache_key)
        if cached and (now - cached[0]) < 15:
            return cached[1]

    symbols = _index_constituents(db, index, datetime.now().date())
    if not symbols:
        return {"data": [], "is_live": False, "message": "No constituents found for index"}

    is_market_open = live_market.is_market_open()
    gainers = _live_gainers(symbols) if is_market_open else []

    # If no live gainers (e.g. market just opened or closed), use yfinance
    if not gainers:
        gainers = _yfinance_gainers(symbols)

    # Sort and limit
    gainers.sort(key=lambda x: x["change_pct"], reverse=True)

    result = {"data": gainers[:limit], "is_live": is_market_open, "count": len(gainers)}

    with _top_gainers_cache_lock:
        _top_gainers_cache[cache_key] = (now, result)
    return result


def _index_constituents(db: Session, index: str, current_date: date) -> list[str]:
    """Symbols in the index on the given date."""
    symbols_query = (
        db.query(IndexConstituentHistory.symbol)
        .join(
            IndexUniverseDefinition,
            IndexConstituentHistory.universe_id == IndexUniverseDefinition.id,
        )
        .filter(
            IndexUniverseDefinition.index_code == index,
            IndexConstituentHistory.effective_from <= current_date,
            or_(
                IndexConstituentHistory.effective_to.is_(None),
                IndexConstituentHistory.effective_to >= current_date,
            ),
        )
        .all()
    )
    return [s[0] for s in symbols_query]


def _live_gainers(symbols: list[str]) -> list[dict[str, Any]]:
    """Symbols with a positive change in the live tick cache."""
    gainers = []
    for symbol, tick in live_market.get_latest_ticks(symbols).items():
        change_pct = tick.get("chp") or tick.get("change_pct", 0)
        if change_pct > 0:
            gainers.append(
                {
                    "symbol": symbol,
                    "price": tick.get("ltp") or tick.get("price"),
                    "change_pct": change_pct,
                    "source": "live",
                }
            )
    return gainers


def _yfinance_gainers(symbols: list[str]) -> list[dict[str, Any]]:
    """Symbols with a positive yfinance change (first 50, to avoid rate limits)."""
    gainers = []
    for symbol, quote in yfinance_service.get_quotes(symbols[:50]).items():
        if quote and quote.get("change_pct", 0) > 0:
            gainers.append(
                {
                    "symbol": symbol,
                    "price": quote["price"],
                    "change_pct": quote["change_pct"],
                    "source": "yfinance",
                }
            )
    return gainers
