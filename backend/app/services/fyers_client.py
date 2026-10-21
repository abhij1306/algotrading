"""
Legacy Fyers client: the process singleton over HAT's `FyersSession`.

HAT owns the session (credentials from the token file, the SDK model, token validation; K2). This
class adds only what the legacy market code uses: quotes and history. It places no orders and reads
no book (K4). K5b deletes it with the legacy market features.
"""

from collections.abc import Iterable
from dataclasses import replace
from typing import Any, Self

from app.hat.execution.fyers_session import FyersSession

from ..utils.logger import get_logger
from .fyers.quotes import Quote, parse_quotes
from .symbol_master import symbol_master

# Setup Logging
logger = get_logger("services.fyers_client")

# Most symbols the Fyers quotes endpoint accepts in one call.
QUOTES_BATCH_SIZE = 50


def _to_fyers_symbols(symbols: Iterable[str]) -> dict[str, list[str]]:
    """{Fyers symbol: the requested symbols that map to it}, in request order.

    Two requests can share a Fyers symbol (``LTIM`` and ``LTM`` both give ``NSE:LTM-EQ``);
    unconvertible symbols are dropped.
    """
    converted: dict[str, list[str]] = {}
    for symbol in dict.fromkeys(symbols):
        if not symbol:
            continue
        try:
            converted.setdefault(symbol_master.to_fyers(symbol), []).append(symbol)
        except ValueError:
            logger.warning("Skipping quote for unconvertible symbol %r", symbol)
    return converted


def _keyed_by_request(quote: Quote, asked_by_db: dict[str, list[str]]) -> list[Quote]:
    """``quote`` once under each symbol it was asked for (its DB symbol when unmatched)."""
    asked = asked_by_db.get(quote.symbol, [quote.symbol])
    return [quote if symbol == quote.symbol else replace(quote, symbol=symbol) for symbol in asked]


class FyersClient(FyersSession):
    """
    Singleton Fyers Client wrapper.
    Ensures valid session and handles API calls.
    """

    _instance = None

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        super().__init__()
        self._initialized = True

    def quotes(self, symbols: Iterable[str]) -> dict[str, Quote]:
        """Quotes keyed by the requested DB symbol; symbols Fyers has no quote for are absent.

        Symbols are converted with SymbolMaster (one it cannot convert is skipped) and sent in
        batches of QUOTES_BATCH_SIZE (the Fyers per-call limit). A reply for a renamed symbol
        (asked as ``LTIM``, answered as ``LTM``) is keyed by each symbol asked for. A batch
        whose request fails is skipped; this never raises.
        """
        model = self.fyers
        if model is None:
            return {}
        requested = _to_fyers_symbols(symbols)
        if not requested:
            return {}

        fyers_symbols = list(requested)
        asked_by_db = {symbol_master.to_db(f): asked for f, asked in requested.items()}
        quotes: dict[str, Quote] = {}
        for start in range(0, len(fyers_symbols), QUOTES_BATCH_SIZE):
            batch = fyers_symbols[start : start + QUOTES_BATCH_SIZE]
            try:
                response = model.quotes({"symbols": ",".join(batch)})
            except Exception:
                logger.exception("Fyers quotes request failed for %d symbols", len(batch))
                continue
            for quote in parse_quotes(response).values():
                for keyed in _keyed_by_request(quote, asked_by_db):
                    quotes[keyed.symbol] = keyed
        return quotes

    def get_historical_data(
        self, symbol: str, timeframe: str, range_from: str, range_to: str
    ) -> dict[str, Any]:
        """Fetch historical candle data"""
        if not self.fyers:
            return {}

        data = {
            "symbol": symbol,
            "resolution": timeframe,
            "date_format": "1",
            "range_from": range_from,
            "range_to": range_to,
            "cont_flag": "1",
        }

        try:
            response = self.fyers.history(data)
            return response
        except Exception as e:
            logger.exception(f"Error fetching history for {symbol}: {e}")
            return {}


# Global Accessor - LAZY INITIALIZATION
_fyers_client = None


def get_fyers_client() -> FyersClient:
    global _fyers_client
    if _fyers_client is None:
        _fyers_client = FyersClient()
    return _fyers_client


def reset_fyers_client() -> None:
    """Reset singleton so fresh credentials are loaded on next access."""
    global _fyers_client
    _fyers_client = None
    FyersClient._instance = None
