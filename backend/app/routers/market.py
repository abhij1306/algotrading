import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.symbol_master import symbol_master
from ..utils.market_hours import get_market_status

logger = logging.getLogger(__name__)
router = APIRouter()
DB_DEPENDENCY = Depends(get_db)
NIFTY_50_NAME = "NIFTY 50"


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
        from ..services.fyers_client import get_fyers_client

        fyers = get_fyers_client()

        if not fyers or not fyers.fyers:
            return []

        # Index symbols
        indices = {
            NIFTY_50_NAME: {"fyers_symbol": "NSE:NIFTY50-INDEX", "symbol": "NIFTY50"},
            "SENSEX": {"fyers_symbol": "BSE:SENSEX-INDEX", "symbol": "SENSEX"},
            "BANKNIFTY": {"fyers_symbol": "NSE:NIFTYBANK-INDEX", "symbol": "BANKNIFTY"},
            "NIFTY IT": {"fyers_symbol": "NSE:NIFTYIT-INDEX", "symbol": "NIFTYIT"},
        }

        results = []

        for name, entry in indices.items():
            try:
                symbol = entry["fyers_symbol"]
                quote = fyers.get_parsed_quotes([symbol])
                if not quote:
                    continue

                # get_parsed_quotes can return either fyers-format key or DB-format key
                data = quote.get(symbol) or quote.get(symbol_master.to_db(symbol))
                if not data:
                    continue

                ltp = data.get("ltp")
                prev_close = data.get("prev_close")
                if ltp is None or prev_close in (None, 0):
                    continue

                change = ltp - prev_close
                change_pct = change / prev_close * 100

                results.append(
                    {
                        "name": name,
                        "symbol": entry["symbol"],
                        "value": ltp,
                        "change": change,
                        "changePercent": change_pct,
                    }
                )
            except Exception:
                logger.exception("Failed to fetch index quote for %s", name)
                continue

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

        # Try to get live quotes from Fyers
        from ..services.fyers_client import get_fyers_client

        fyers = get_fyers_client()

        quotes = {}

        if fyers and fyers.fyers:
            # Convert to Fyers format
            symbol_list = [symbol_master.to_fyers(s) for s in raw_symbols]
            fyers_quotes = fyers.get_parsed_quotes(symbol_list)

            # Convert keys back to DB format
            for fyers_sym, data in fyers_quotes.items():
                db_sym = symbol_master.to_db(fyers_sym)
                quotes[db_sym] = data

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
        logger.warning("Error fetching quotes: %s", e)
        return {}


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
    from ..database import Company

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
# Stop adding strikes once this many option rows have been collected
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
    """Add call/put rows strike by strike until the running total reaches the limit."""
    expiry = chain.expiry.isoformat() if chain.expiry else None
    for strike in chain.strikes:
        if strike.call:
            option_results.append(
                _option_row(underlying, strike.strike_price, strike.call, "CE", "Call", expiry)
            )
        if strike.put:
            option_results.append(
                _option_row(underlying, strike.strike_price, strike.put, "PE", "Put", expiry)
            )
        # Limit options per underlying
        if len(option_results) >= MAX_OPTION_SEARCH_ROWS:
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
    from ..database import Company

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
    from ..database import Watchlist

    items = db.query(Watchlist).order_by(Watchlist.added_at.desc()).all()
    # For each symbol, fetch latest price/data
    return [_watchlist_row(db, item) for item in items]


def _watchlist_row(db: Session, item: object) -> dict[str, Any]:
    """Watchlist entry with name and latest stored close vs. that day's open."""
    from ..database import Company

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
    from ..database import HistoricalPrice

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
    from ..database import Watchlist

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
    from ..database import Watchlist

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
        from ..services.fyers_client import get_fyers_client
        from ..services.universe import get_universe_service

        # Get index constituents
        symbols = get_universe_service().get_symbols(index)
        if not symbols:
            return {"gainers": [], "losers": []}

        # Get Fyers client
        fyers = get_fyers_client()
        if not fyers or not fyers.fyers:
            return {"gainers": [], "losers": []}

        # Convert to Fyers format
        fyers_symbols = [symbol_master.to_fyers(s) for s in symbols]

        # Fetch quotes in batches (Fyers has limits)
        all_quotes = {}
        batch_size = 50
        for i in range(0, len(fyers_symbols), batch_size):
            batch = fyers_symbols[i : i + batch_size]
            quotes = fyers.get_parsed_quotes(batch)
            all_quotes.update(quotes)

        # Calculate changes and sort
        movers = []
        for fyers_sym, data in all_quotes.items():
            db_sym = symbol_master.to_db(fyers_sym)
            ltp = data.get("ltp", 0)
            prev_close = data.get("prev_close", ltp)

            if prev_close and ltp:
                change_pct = ((ltp - prev_close) / prev_close) * 100
                movers.append(
                    {
                        "symbol": db_sym,
                        "name": data.get("name", db_sym),
                        "price": round(ltp, 2),
                        "changePercent": round(change_pct, 2),
                    }
                )

        # Sort by change percent
        movers.sort(key=lambda x: x["changePercent"], reverse=True)

        # Get top gainers and losers
        gainers = movers[:limit]
        losers = movers[-limit:][::-1]  # Reverse to show worst first

        return {"gainers": gainers, "losers": losers}

    except Exception as e:
        logger.exception(f"Error fetching top movers: {e}")
        return {"gainers": [], "losers": []}


@router.get("/sector-performance", response_model=None)
def get_sector_performance() -> list[dict[str, Any]]:
    """
    Get sector index performance (Banking, IT, Pharma, Auto, Metal).
    Returns real-time data from Fyers during market hours.
    """
    try:
        from ..services.fyers_client import get_fyers_client
        from ..services.symbol_master import symbol_master

        # Sector indices mapping (Fyers format)
        sectors = {
            "Banking": {"fyers_symbol": "NSE:NIFTYBANK-INDEX", "symbol": "BANKNIFTY"},
            "IT": {"fyers_symbol": "NSE:NIFTYIT-INDEX", "symbol": "NIFTYIT"},
            "Pharma": {"fyers_symbol": "NSE:NIFTYPHARMA-INDEX", "symbol": "NIFTYPHARMA"},
            "Auto": {"fyers_symbol": "NSE:NIFTYAUTO-INDEX", "symbol": "NIFTYAUTO"},
            "Metal": {"fyers_symbol": "NSE:NIFTYMETAL-INDEX", "symbol": "NIFTYMETAL"},
        }

        fyers = get_fyers_client()
        if not fyers or not fyers.fyers:
            logger.warning("Fyers client not available for sector performance")
            return []

        # Fetch quotes
        fyers_symbols = [entry["fyers_symbol"] for entry in sectors.values()]
        logger.debug("Fetching sector quotes for: %s", fyers_symbols)
        quotes = fyers.get_parsed_quotes(fyers_symbols)
        logger.debug("Received %d sector quotes", len(quotes))

        results = []
        for name, entry in sectors.items():
            fyers_symbol = entry["fyers_symbol"]
            # Convert to DB format for lookup (get_parsed_quotes returns DB format keys)
            db_symbol = symbol_master.to_db(fyers_symbol)

            if db_symbol in quotes:
                data = quotes[db_symbol]
                ltp = data.get("ltp", 0)
                prev_close = data.get("prev_close", ltp)
                change = ltp - prev_close
                change_pct = (change / prev_close * 100) if prev_close else 0

                results.append(
                    {
                        "name": name,
                        "symbol": entry["symbol"],
                        "value": round(ltp, 2),
                        "changePercent": round(change_pct, 2),
                    }
                )
                logger.debug("Sector %s: %s (%.2f%%)", name, ltp, change_pct)
            else:
                logger.debug(
                    "No data for sector %s (Fyers: %s, DB: %s).",
                    name,
                    fyers_symbol,
                    db_symbol,
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
