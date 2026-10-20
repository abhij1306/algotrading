import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.fyers.quotes import Quote
from ..services.symbol_master import symbol_master
from ..utils.db_errors import is_db_unavailable
from ..utils.market_hours import get_market_status

logger = logging.getLogger(__name__)
router = APIRouter()
DB_DEPENDENCY = Depends(get_db)
NIFTY_50_NAME = "NIFTY 50"


def _fyers_quotes(symbols: list[str]) -> dict[str, Quote]:
    """Live Fyers quotes keyed by DB symbol, through the gateway; {} when it is unavailable."""
    from ..services.fyers_client import get_fyers_client

    fyers = get_fyers_client()
    return fyers.quotes(symbols) if fyers else {}


def _percent(quote: Quote) -> float:
    """The quote's change percent rounded to 2 places; 0 when the previous close is unknown."""
    return round(float(quote.change_pct), 2) if quote.change_pct is not None else 0.0


@router.get("/market/status", response_model=None)
def market_status() -> dict[str, Any]:
    """Get current market status (open/closed)"""
    return get_market_status()


@router.get("/market/indices", response_model=None)
def get_market_indices() -> list[dict[str, Any]]:
    """
    Get major market indices data.
    Returns NIFTY 50, SENSEX, BANKNIFTY, NIFTY IT
    """
    try:
        # Index name -> DB symbol
        indices = {
            NIFTY_50_NAME: "NIFTY50",
            "SENSEX": "SENSEX",
            "BANKNIFTY": "BANKNIFTY",
            "NIFTY IT": "NIFTYIT",
        }
        quotes = _fyers_quotes(list(indices.values()))

        results = []
        for name, symbol in indices.items():
            quote = quotes.get(symbol)
            if quote is None or quote.ltp is None or quote.change is None:
                continue  # no price or no previous close
            results.append(
                {
                    "name": name,
                    "symbol": symbol,
                    "value": float(quote.ltp),
                    "change": float(quote.change),
                    "changePercent": float(quote.change_pct or 0),
                }
            )

        return results
    except Exception:
        logger.exception("Error fetching market indices")
        return []


@router.get("/status", response_model=None)
def market_status_alias() -> dict[str, Any]:
    """Alias for /market/status to keep API clients consistent."""
    return market_status()


@router.get("/indices", response_model=None)
def get_market_indices_alias() -> list[dict[str, Any]]:
    """Alias for /market/indices to keep API clients consistent."""
    return get_market_indices()


@router.get("/quotes/live", response_model=None)
def get_live_quotes(symbols: str, db: Session = DB_DEPENDENCY) -> dict[str, Any]:
    """
    Get live quotes for multiple symbols.
    Returns live data if available, otherwise falls back to last known prices.
    symbols: comma-separated list (e.g., "RELIANCE,TCS,INFY")
    """
    try:
        raw_symbols = [s.strip() for s in symbols.split(",") if s.strip()]
        if not raw_symbols:
            return {}

        # Live quotes from Fyers; a symbol without a last price is left out
        quotes = {
            symbol: _live_quote_row(quote)
            for symbol, quote in _fyers_quotes(raw_symbols).items()
            if quote.ltp is not None
        }

        # If no live data, fall back to database
        if not quotes:
            from ..models import Company, HistoricalPrice

            # Get latest prices from database
            latest_prices = (
                db.query(Company.symbol, HistoricalPrice.close, HistoricalPrice.volume)
                .join(HistoricalPrice, Company.id == HistoricalPrice.company_id)
                .filter(Company.symbol.in_(raw_symbols))
                .order_by(HistoricalPrice.date.desc())
                .limit(len(raw_symbols))
                .all()
            )

            for sym, close, vol in latest_prices:
                quotes[sym] = {
                    "ltp": close or 0,
                    "volume": vol or 0,
                    "change": 0,
                    "change_pct": 0,
                    "source": "database",
                }

        return quotes

    except Exception as e:
        if is_db_unavailable(e):
            raise  # an empty map would read as "no quotes"; the API answers 503
        logger.warning("Error fetching quotes: %s", e)
        return {}


def _live_quote_row(quote: Quote) -> dict[str, Any]:
    """A /quotes/live row: missing prices read 0 and a missing previous close reads as the ltp."""
    ltp = float(quote.ltp)
    return {
        "ltp": ltp,
        "volume": quote.volume or 0,
        "high": float(quote.high or 0),
        "low": float(quote.low or 0),
        "open": float(quote.open or 0),
        "prev_close": float(quote.prev_close) if quote.prev_close is not None else ltp,
        "change_pct": _percent(quote),
    }


@router.get("/quote/{symbol}", response_model=None)
def get_single_quote(symbol: str) -> dict[str, Any]:
    """
    Get quote for a single symbol (supports Yahoo Finance symbols like ^GSPC).
    Used for fetching US indices and other Yahoo Finance data.
    """
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="2d")

        if hist.empty:
            return {"symbol": symbol, "price": 0, "changePercent": 0}

        current = hist["Close"].iloc[-1]
        prev = hist["Close"].iloc[-2] if len(hist) >= 2 else hist["Open"].iloc[-1]
        change = current - prev
        change_pct = (change / prev * 100) if prev else 0

        return {
            "symbol": symbol,
            "price": round(current, 2),
            "change": round(change, 2),
            "changePercent": round(change_pct, 2),
        }

    except Exception as e:
        logger.exception(f"Error fetching quote for {symbol}: {e}")
        return {"symbol": symbol, "price": 0, "changePercent": 0}


@router.get("/search", response_model=None)
def search_symbols(
    query: str,
    exclude_indices: bool = False,
    include_options: bool = True,
    db: Session = DB_DEPENDENCY,
) -> list[dict[str, Any]]:
    """
    Search for companies, indices, and option symbols by symbol or name
    exclude_indices: Set to True to only return equities (for analyst mode)
    include_options: Set to False to exclude option symbols
    """
    if not query or len(query) < 2:
        return []

    from ..constants.indices import STOCK_INDICES

    results_list = []
    query_upper = query.upper()

    # Search in indices first (skip if exclude_indices=True)
    if not exclude_indices:
        for idx_key, idx_info in STOCK_INDICES.items():
            idx_name = idx_info.get("name", "")
            if query_upper in idx_key.upper() or query_upper in idx_name.upper():
                results_list.append(
                    {"symbol": idx_key, "name": idx_name, "sector": "INDEX", "type": "INDEX"}
                )

    # Search in Company table (equities only)
    from ..models import Company

    companies = (
        db.query(Company)
        .filter((Company.symbol.ilike(f"%{query}%")) | (Company.name.ilike(f"%{query}%")))
        .limit(10)
        .all()
    )

    for c in companies:
        results_list.append(
            {"symbol": c.symbol, "name": c.name, "sector": c.sector, "type": "EQUITY"}
        )

    # Search for option symbols if enabled and query matches index/stock names
    if include_options and len(query_upper) >= 2:
        option_results = _search_option_symbols(query_upper, results_list)
        results_list.extend(option_results)

    return results_list[:20]  # Limit to 20 total results


# Optionable underlyings (indices + top stocks) and their display names
OPTIONABLE_UNDERLYINGS = {
    "NIFTY": NIFTY_50_NAME,
    "NIFTY50": NIFTY_50_NAME,
    "BANKNIFTY": "NIFTY Bank",
    "FINNIFTY": "NIFTY Financial Services",
    "MIDCPNIFTY": "NIFTY Midcap Select",
    "RELIANCE": "Reliance Industries",
    "TCS": "Tata Consultancy Services",
    "INFY": "Infosys",
    "HDFCBANK": "HDFC Bank",
    "ICICIBANK": "ICICI Bank",
    "SBIN": "State Bank of India",
    "TATAMOTORS": "Tata Motors",
    "TATASTEEL": "Tata Steel",
    "AXISBANK": "Axis Bank",
    "KOTAKBANK": "Kotak Mahindra Bank",
    "ITC": "ITC Limited",
    "LT": "Larsen & Toubro",
    "HINDUNILVR": "Hindustan Unilever",
}
# Stop adding strikes of one underlying once it has this many option rows
MAX_OPTION_SEARCH_ROWS = 12


def _search_option_symbols(query: str, existing_results: list) -> list:
    """
    Search for option symbols based on the query.
    Returns option strikes for matching underlyings.
    """
    option_results: list[dict[str, Any]] = []
    matching_underlyings = _matching_underlyings(query, existing_results)
    if not matching_underlyings:
        return option_results

    try:
        from ..services.option_chain_service import option_chain_service

        for underlying in matching_underlyings[:3]:  # Limit to 3 underlyings
            try:
                chain = option_chain_service.get_option_chain(
                    underlying=underlying,
                    strike_count=6,  # Get ATM ± 3 strikes
                )
                if chain and chain.strikes:
                    _append_chain_options(underlying, chain, option_results)
            except Exception:
                # Skip this underlying if its option chain fetch fails
                logger.debug("Option chain search failed for %s", underlying, exc_info=True)

    except Exception:
        # If option chain service fails, return what we have
        logger.debug("Option chain service unavailable for search", exc_info=True)

    return option_results


def _matching_underlyings(query: str, existing_results: list) -> list[str]:
    """Underlyings whose code or name contains the query, then optionable result symbols."""
    matching = [
        underlying
        for underlying, name in OPTIONABLE_UNDERLYINGS.items()
        if query in underlying or query in name.upper()
    ]
    # Also check if any existing result is an index or equity that has options
    for result in existing_results:
        if result.get("type") in ["INDEX", "EQUITY"]:
            symbol = result.get("symbol", "")
            if symbol in OPTIONABLE_UNDERLYINGS and symbol not in matching:
                matching.append(symbol)
    return matching


def _append_chain_options(
    underlying: str, chain: object, option_results: list[dict[str, Any]]
) -> None:
    """Add call/put rows strike by strike until this underlying has MAX_OPTION_SEARCH_ROWS.

    The limit is per underlying, so a long first chain cannot crowd out the others.
    """
    expiry = chain.expiry.isoformat() if chain.expiry else None
    start = len(option_results)
    for strike in chain.strikes:
        if strike.call:
            option_results.append(
                _option_row(underlying, strike.strike_price, strike.call, "CE", "Call", expiry)
            )
        if strike.put:
            option_results.append(
                _option_row(underlying, strike.strike_price, strike.put, "PE", "Put", expiry)
            )
        if len(option_results) - start >= MAX_OPTION_SEARCH_ROWS:
            break


def _option_row(
    underlying: str,
    strike_price: float,
    leg: object,
    option_type: str,
    label: str,
    expiry: str | None,
) -> dict[str, Any]:
    return {
        "symbol": leg.symbol,
        "name": f"{underlying} {strike_price} {option_type} ({label})",
        "sector": "OPTIONS",
        "type": option_type,
        "underlying": underlying,
        "strike": strike_price,
        "expiry": expiry,
        "ltp": leg.ltp,
        "instrument_type": option_type,
    }


@router.get("/sectors", response_model=None)
def get_sectors(db: Session = DB_DEPENDENCY) -> dict[str, Any]:
    """
    Get list of all available sectors
    """
    from ..models import Company

    # query distinct sectors
    sectors = (
        db.query(Company.sector)
        .distinct()
        .filter(Company.sector.is_not(None))
        .order_by(Company.sector)
        .all()
    )

    return {"sectors": [s[0] for s in sectors]}


@router.get("/watchlist", response_model=None)
def get_watchlist(db: Session = DB_DEPENDENCY) -> list[dict[str, Any]]:
    """Get user watchlist"""
    from ..models import Watchlist

    items = db.query(Watchlist).order_by(Watchlist.added_at.desc()).all()
    # For each symbol, fetch latest price/data
    return [_watchlist_row(db, item) for item in items]


def _watchlist_row(db: Session, item: object) -> dict[str, Any]:
    """Watchlist entry with name and latest stored close vs. that day's open."""
    from ..models import Company

    # Determine strict symbol for query
    sym = symbol_master.to_db(item.symbol)
    name = item.symbol
    ltp = change = change_pct = None

    company = db.query(Company).filter(Company.symbol == sym).first()
    if company:
        name = company.name or item.symbol
        ltp, change, change_pct = _latest_close_change(db, company.id)

    return {
        "symbol": sym,
        "name": name,
        "price": ltp,
        "ltp": ltp,
        "change": round(change, 2) if isinstance(change, (int, float)) else None,
        "changePercent": round(change_pct, 2) if isinstance(change_pct, (int, float)) else None,
        "instrument_type": item.instrument_type,
    }


def _latest_close_change(
    db: Session, company_id: int
) -> tuple[float | None, float | None, float | None]:
    """(close, change, change %) of the latest historical bar; change is vs. its open."""
    from ..models import HistoricalPrice

    latest_price = (
        db.query(HistoricalPrice)
        .filter(HistoricalPrice.company_id == company_id)
        .order_by(HistoricalPrice.date.desc())
        .first()
    )
    if not latest_price:
        return None, None, None
    ltp = latest_price.close
    prev = latest_price.open  # Approximate previous close
    if not prev:
        return ltp, None, None
    change = ltp - prev
    return ltp, change, change / prev * 100


@router.post(
    "/watchlist",
    response_model=None,
    responses={400: {"description": "Symbol required"}},
)
def add_to_watchlist(item: dict, db: Session = DB_DEPENDENCY) -> dict[str, Any]:
    """Add to watchlist"""
    from ..models import Watchlist

    symbol = item.get("symbol")
    inst_type = item.get("instrument_type", "EQ")

    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol required")

    existing = db.query(Watchlist).filter(Watchlist.symbol == symbol).first()
    if existing:
        return {"message": "Already in watchlist"}

    new_item = Watchlist(symbol=symbol, instrument_type=inst_type)
    db.add(new_item)
    db.commit()
    return {"message": "Added"}


@router.delete("/watchlist/{symbol}", response_model=None)
def remove_from_watchlist(symbol: str, db: Session = DB_DEPENDENCY) -> dict[str, Any]:
    """Remove from watchlist"""
    from ..models import Watchlist

    db.query(Watchlist).filter(Watchlist.symbol == symbol).delete()
    db.commit()
    return {"message": "Removed"}


@router.get("/top-movers", response_model=None)
def get_top_movers(index: str = "NIFTY50", limit: int = 5) -> dict[str, Any]:
    """
    Get top gainers and losers from an index.
    Returns real-time data from Fyers during market hours.

    Args:
        index: Index ID (e.g., NIFTY50, NIFTYBANK)
        limit: Number of top gainers/losers to return
    """
    try:
        from ..services.universe import get_universe_service

        # Get index constituents
        symbols = get_universe_service().get_symbols(index)
        if not symbols:
            return {"gainers": [], "losers": []}

        # Fetch quotes (the gateway batches them to the Fyers per-call limit)
        movers = [
            {
                "symbol": symbol,
                "name": symbol,
                "price": round(float(quote.ltp), 2),
                "changePercent": _percent(quote),
            }
            for symbol, quote in _fyers_quotes(symbols).items()
            if quote.ltp is not None
        ]

        # Sort by change percent
        movers.sort(key=lambda x: x["changePercent"], reverse=True)

        # Get top gainers and losers
        gainers = movers[:limit]
        losers = movers[-limit:][::-1]  # Reverse to show worst first

        return {"gainers": gainers, "losers": losers}

    except Exception as e:
        if is_db_unavailable(e):
            raise  # empty lists would read as "no movers today"; the API answers 503
        logger.exception(f"Error fetching top movers: {e}")
        return {"gainers": [], "losers": []}


@router.get("/sector-performance", response_model=None)
def get_sector_performance() -> list[dict[str, Any]]:
    """
    Get sector index performance (Banking, IT, Pharma, Auto, Metal).
    Returns real-time data from Fyers during market hours.
    """
    try:
        # Sector name -> DB symbol of its index
        sectors = {
            "Banking": "BANKNIFTY",
            "IT": "NIFTYIT",
            "Pharma": "NIFTYPHARMA",
            "Auto": "NIFTYAUTO",
            "Metal": "NIFTYMETAL",
        }

        quotes = _fyers_quotes(list(sectors.values()))
        logger.debug("Received %d sector quotes", len(quotes))

        results = []
        for name, symbol in sectors.items():
            quote = quotes.get(symbol)
            if quote is None or quote.ltp is None:
                logger.debug("No data for sector %s (%s).", name, symbol)
                continue
            results.append(
                {
                    "name": name,
                    "symbol": symbol,
                    "value": round(float(quote.ltp), 2),
                    "changePercent": _percent(quote),
                }
            )

        return results

    except Exception as e:
        logger.exception(f"Error fetching sector performance: {e}")
        return []


@router.get("/commodities", response_model=None)
def get_commodities() -> list[dict[str, Any]]:
    """
    Get commodity prices (Gold, Crude Oil) from Yahoo Finance.
    Used during post-market hours.
    """
    try:
        import yfinance as yf

        commodities = {"Gold": "GC=F", "Crude Oil": "CL=F"}

        results = []
        for name, symbol in commodities.items():
            try:
                ticker = yf.Ticker(symbol)
                hist = ticker.history(period="2d")

                if not hist.empty:
                    current = hist["Close"].iloc[-1]
                    prev = hist["Close"].iloc[-2] if len(hist) >= 2 else hist["Open"].iloc[-1]
                    change_pct = ((current - prev) / prev * 100) if prev else 0

                    results.append(
                        {
                            "name": name,
                            "symbol": symbol,
                            "price": round(current, 2),
                            "changePercent": round(change_pct, 2),
                        }
                    )
            except Exception as e:
                logger.warning(f"Failed to fetch {name}: {e}")
                continue

        return results

    except Exception as e:
        logger.exception(f"Error fetching commodities: {e}")
        return []


@router.get("/currency/{pair}", response_model=None)
def get_currency(pair: str) -> dict[str, Any]:
    """
    Get currency exchange rate from Yahoo Finance.
    Example: pair="USDINR" fetches USD/INR rate.
    """
    try:
        import yfinance as yf

        # Convert pair to Yahoo Finance format
        yf_symbol = f"{pair}=X"

        ticker = yf.Ticker(yf_symbol)
        hist = ticker.history(period="2d")

        if hist.empty:
            return {"pair": pair, "rate": 0, "changePercent": 0}

        current = hist["Close"].iloc[-1]
        prev = hist["Close"].iloc[-2] if len(hist) >= 2 else hist["Open"].iloc[-1]
        change_pct = ((current - prev) / prev * 100) if prev else 0

        return {"pair": pair, "rate": round(current, 2), "changePercent": round(change_pct, 2)}

    except Exception as e:
        logger.exception(f"Error fetching currency {pair}: {e}")
        return {"pair": pair, "rate": 0, "changePercent": 0}
