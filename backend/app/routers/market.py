import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.symbol_master import symbol_master
from ..utils.market_hours import get_market_status

logger = logging.getLogger(__name__)
router = APIRouter()
DB_DEPENDENCY = Depends(get_db)

@router.get("/market/status")
def market_status():
    """Get current market status (open/closed)"""
    return get_market_status()


@router.get("/market/indices")
def get_market_indices():
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
            "NIFTY 50": {"fyers_symbol": "NSE:NIFTY50-INDEX", "symbol": "NIFTY50"},
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
                change_pct = (change / prev_close * 100)

                results.append({
                    "name": name,
                    "symbol": entry["symbol"],
                    "value": ltp,
                    "change": change,
                    "changePercent": change_pct
                })
            except Exception:
                logger.exception("Failed to fetch index quote for %s", name)
                continue

        return results
    except Exception:
        logger.exception("Error fetching market indices")
        return []


@router.get("/status")
def market_status_alias():
    """Alias for /market/status to keep API clients consistent."""
    return market_status()


@router.get("/indices")
def get_market_indices_alias():
    """Alias for /market/indices to keep API clients consistent."""
    return get_market_indices()

@router.get("/quotes/live")
def get_live_quotes(symbols: str, db: Session = DB_DEPENDENCY):
    """
    Get live quotes for multiple symbols.
    Returns live data if available, otherwise falls back to last known prices.
    symbols: comma-separated list (e.g., "RELIANCE,TCS,INFY")
    """
    try:
        raw_symbols = [s.strip() for s in symbols.split(',') if s.strip()]
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
            latest_prices = db.query(
                Company.symbol,
                HistoricalPrice.close,
                HistoricalPrice.volume
            ).join(
                HistoricalPrice, Company.id == HistoricalPrice.company_id
            ).filter(
                Company.symbol.in_(raw_symbols)
            ).order_by(
                HistoricalPrice.date.desc()
            ).limit(len(raw_symbols)).all()

            for sym, close, vol in latest_prices:
                quotes[sym] = {
                    'ltp': close or 0,
                    'volume': vol or 0,
                    'change': 0,
                    'change_pct': 0,
                    'source': 'database'
                }

        return quotes

    except Exception as e:
        print(f"Error fetching quotes: {str(e)}")
        return {}


@router.get("/quote/{symbol}")
def get_single_quote(symbol: str):
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

        current = hist['Close'].iloc[-1]
        prev = hist['Close'].iloc[-2] if len(hist) >= 2 else hist['Open'].iloc[-1]
        change = current - prev
        change_pct = (change / prev * 100) if prev else 0

        return {
            "symbol": symbol,
            "price": round(current, 2),
            "change": round(change, 2),
            "changePercent": round(change_pct, 2)
        }

    except Exception as e:
        logger.error(f"Error fetching quote for {symbol}: {e}")
        return {"symbol": symbol, "price": 0, "changePercent": 0}
@router.get("/search")
def search_symbols(
    query: str,
    exclude_indices: bool = False,
    include_options: bool = True,
    db: Session = DB_DEPENDENCY
):
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
            if (query_upper in idx_key.upper() or
                query_upper in idx_name.upper()):
                results_list.append({
                    "symbol": idx_key,
                    "name": idx_name,
                    "sector": "INDEX",
                    "type": "INDEX"
                })

    # Search in Company table (equities only)
    from ..database import Company

    companies = db.query(Company).filter(
        (Company.symbol.ilike(f"%{query}%")) |
        (Company.name.ilike(f"%{query}%"))
    ).limit(10).all()

    for c in companies:
        results_list.append({
            "symbol": c.symbol,
            "name": c.name,
            "sector": c.sector,
            "type": "EQUITY"
        })

    # Search for option symbols if enabled and query matches index/stock names
    if include_options and len(query_upper) >= 2:
        option_results = _search_option_symbols(query_upper, results_list)
        results_list.extend(option_results)

    return results_list[:20]  # Limit to 20 total results


def _search_option_symbols(query: str, existing_results: list) -> list:
    """
    Search for option symbols based on the query.
    Returns option strikes for matching underlyings.
    """
    option_results = []

    # List of optionable underlyings (indices + top stocks)
    optionable_underlyings = {
        "NIFTY": "NIFTY 50",
        "NIFTY50": "NIFTY 50",
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

    # Check if query matches any underlying
    matching_underlyings = []
    for underlying, name in optionable_underlyings.items():
        if query in underlying or query in name.upper():
            matching_underlyings.append(underlying)

    # Also check if any existing result is an index that has options
    for result in existing_results:
        if result.get("type") in ["INDEX", "EQUITY"]:
            symbol = result.get("symbol", "")
            if symbol in optionable_underlyings and symbol not in matching_underlyings:
                matching_underlyings.append(symbol)

    # Fetch option chain for matching underlyings
    if matching_underlyings:
        try:
            from ..services.option_chain_service import option_chain_service

            for underlying in matching_underlyings[:3]:  # Limit to 3 underlyings
                try:
                    chain = option_chain_service.get_option_chain(
                        underlying=underlying,
                        strike_count=6  # Get ATM ± 3 strikes
                    )

                    if chain and chain.strikes:
                        # Add option symbols for strikes around ATM
                        for strike in chain.strikes:
                            # Call option
                            if strike.call:
                                option_results.append({
                                    "symbol": strike.call.symbol,
                                    "name": f"{underlying} {strike.strike_price} CE (Call)",
                                    "sector": "OPTIONS",
                                    "type": "CE",
                                    "underlying": underlying,
                                    "strike": strike.strike_price,
                                    "expiry": chain.expiry.isoformat() if chain.expiry else None,
                                    "ltp": strike.call.ltp,
                                    "instrument_type": "CE"
                                })

                            # Put option
                            if strike.put:
                                option_results.append({
                                    "symbol": strike.put.symbol,
                                    "name": f"{underlying} {strike.strike_price} PE (Put)",
                                    "sector": "OPTIONS",
                                    "type": "PE",
                                    "underlying": underlying,
                                    "strike": strike.strike_price,
                                    "expiry": chain.expiry.isoformat() if chain.expiry else None,
                                    "ltp": strike.put.ltp,
                                    "instrument_type": "PE"
                                })

                            # Limit options per underlying
                            if len(option_results) >= 12:
                                break

                except Exception:
                    # Silently skip if option chain fetch fails
                    continue

        except Exception:
            # If option chain service fails, return empty
            pass

    return option_results

@router.get("/sectors")
def get_sectors(db: Session = DB_DEPENDENCY):
    """
    Get list of all available sectors
    """
    from ..database import Company

    # query distinct sectors
    sectors = db.query(Company.sector).distinct().filter(Company.sector.is_not(None)).order_by(Company.sector).all()

    return {"sectors": [s[0] for s in sectors]}

@router.get("/watchlist")
def get_watchlist(db: Session = DB_DEPENDENCY):
    """Get user watchlist"""
    from ..database import Company, HistoricalPrice, Watchlist

    # Get symbols
    items = db.query(Watchlist).order_by(Watchlist.added_at.desc()).all()
    if not items:
        return []

    results = []
    # For each symbol, fetch latest price/data
    for item in items:
        # Try finding basic info
        ltp = None
        change = None
        change_pct = None
        name = item.symbol  # Default to symbol

        # Determine strict symbol for query
        sym = symbol_master.to_db(item.symbol)

        # Try finding live price from historical (or fyers if we had it connected here)
        # For now, get latest historical
        company = db.query(Company).filter(Company.symbol == sym).first()
        if company:
            name = company.name or item.symbol  # Use company name if available
            latest_price = (
                db.query(HistoricalPrice)
                .filter(HistoricalPrice.company_id == company.id)
                .order_by(HistoricalPrice.date.desc())
                .first()
            )
            if latest_price:
                ltp = latest_price.close
                # Calculate change (vs prev close approx)
                prev = latest_price.open # Approximate
                if prev:
                    change = ltp - prev
                    change_pct = (change / prev * 100)

        results.append({
            "symbol": sym,
            "name": name,
            "price": ltp,
            "ltp": ltp,
            "change": round(change, 2) if isinstance(change, (int, float)) else None,
            "changePercent": round(change_pct, 2) if isinstance(change_pct, (int, float)) else None,
            "instrument_type": item.instrument_type
        })

    return results

@router.post("/watchlist")
def add_to_watchlist(item: dict, db: Session = DB_DEPENDENCY):
    """Add to watchlist"""
    from ..database import Watchlist

    symbol = item.get('symbol')
    inst_type = item.get('instrument_type', 'EQ')

    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol required")

    existing = db.query(Watchlist).filter(Watchlist.symbol == symbol).first()
    if existing:
        return {"message": "Already in watchlist"}

    new_item = Watchlist(symbol=symbol, instrument_type=inst_type)
    db.add(new_item)
    db.commit()
    return {"message": "Added"}

@router.delete("/watchlist/{symbol}")
def remove_from_watchlist(symbol: str, db: Session = DB_DEPENDENCY):
    """Remove from watchlist"""
    from ..database import Watchlist

    db.query(Watchlist).filter(Watchlist.symbol == symbol).delete()
    db.commit()
    return {"message": "Removed"}


@router.get("/top-movers")
def get_top_movers(index: str = "NIFTY50", limit: int = 5):
    """
    Get top gainers and losers from an index.
    Returns real-time data from Fyers during market hours.

    Args:
        index: Index ID (e.g., NIFTY50, NIFTYBANK)
        limit: Number of top gainers/losers to return
    """
    try:
        from ..services.fyers_client import get_fyers_client
        from ..services.index_universe_loader import index_universe_loader

        # Get index constituents
        symbols = index_universe_loader.get_index_symbols(index)
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
            batch = fyers_symbols[i:i + batch_size]
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
                movers.append({
                    "symbol": db_sym,
                    "name": data.get("name", db_sym),
                    "price": round(ltp, 2),
                    "changePercent": round(change_pct, 2)
                })

        # Sort by change percent
        movers.sort(key=lambda x: x["changePercent"], reverse=True)

        # Get top gainers and losers
        gainers = movers[:limit]
        losers = movers[-limit:][::-1]  # Reverse to show worst first

        return {
            "gainers": gainers,
            "losers": losers
        }

    except Exception as e:
        logger.error(f"Error fetching top movers: {e}")
        return {"gainers": [], "losers": []}


@router.get("/sector-performance")
def get_sector_performance():
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
        logger.info(f"Fetching sector quotes for: {fyers_symbols}")
        quotes = fyers.get_parsed_quotes(fyers_symbols)
        logger.info(f"Received {len(quotes)} sector quotes with keys: {list(quotes.keys())}")

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

                results.append({
                    "name": name,
                    "symbol": entry["symbol"],
                    "value": round(ltp, 2),
                    "changePercent": round(change_pct, 2)
                })
                logger.info(f"Sector {name}: {ltp} ({change_pct:.2f}%)")
            else:
                logger.warning(f"No data for sector {name} (Fyers: {fyers_symbol}, DB: {db_symbol}). Available keys: {list(quotes.keys())}")

        return results

    except Exception as e:
        logger.error(f"Error fetching sector performance: {e}", exc_info=True)
        return []


@router.get("/commodities")
def get_commodities():
    """
    Get commodity prices (Gold, Crude Oil) from Yahoo Finance.
    Used during post-market hours.
    """
    try:
        import yfinance as yf

        commodities = {
            "Gold": "GC=F",
            "Crude Oil": "CL=F"
        }

        results = []
        for name, symbol in commodities.items():
            try:
                ticker = yf.Ticker(symbol)
                hist = ticker.history(period="2d")

                if not hist.empty:
                    current = hist['Close'].iloc[-1]
                    prev = hist['Close'].iloc[-2] if len(hist) >= 2 else hist['Open'].iloc[-1]
                    change_pct = ((current - prev) / prev * 100) if prev else 0

                    results.append({
                        "name": name,
                        "symbol": symbol,
                        "price": round(current, 2),
                        "changePercent": round(change_pct, 2)
                    })
            except Exception as e:
                logger.warning(f"Failed to fetch {name}: {e}")
                continue

        return results

    except Exception as e:
        logger.error(f"Error fetching commodities: {e}")
        return []


@router.get("/currency/{pair}")
def get_currency(pair: str):
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

        current = hist['Close'].iloc[-1]
        prev = hist['Close'].iloc[-2] if len(hist) >= 2 else hist['Open'].iloc[-1]
        change_pct = ((current - prev) / prev * 100) if prev else 0

        return {
            "pair": pair,
            "rate": round(current, 2),
            "changePercent": round(change_pct, 2)
        }

    except Exception as e:
        logger.error(f"Error fetching currency {pair}: {e}")
        return {"pair": pair, "rate": 0, "changePercent": 0}
