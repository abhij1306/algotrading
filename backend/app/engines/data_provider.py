"""
Data Provider
=============
Provides market data for backtesting and live trading.
"""

import logging
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from ..database import get_db_session

logger = logging.getLogger(__name__)


class DataProvider:
    """
    Provides efficient OHLCV data for backtesting.
    """

    def __init__(self, db: Session | None = None):
        self.db = db or get_db_session()

    def get_intraday_data(
        self, symbols: list[str], target_date: date, _timeframe: int = 5
    ) -> dict[str, list]:
        """Get intraday data for symbols on a given date"""
        from ..models.price import IntradayCandle

        data_by_symbol = {symbol: [] for symbol in symbols}

        start_time = datetime.combine(target_date, datetime.min.time())
        end_time = datetime.combine(target_date, datetime.max.time())

        candles = (
            self.db.query(IntradayCandle)
            .filter(
                IntradayCandle.symbol.in_(symbols),
                IntradayCandle.timestamp >= start_time,
                IntradayCandle.timestamp <= end_time,
            )
            .all()
        )

        for candle in candles:
            data_by_symbol[candle.symbol].append(
                {
                    "timestamp": candle.timestamp,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                }
            )

        return data_by_symbol

    def get_daily_data(
        self, symbols: list[str], start_date: date, end_date: date
    ) -> dict[str, list]:
        """Get daily data for symbols in date range"""
        from ..models.price import HistoricalPrice

        data_by_symbol = {symbol: [] for symbol in symbols}

        prices = (
            self.db.query(HistoricalPrice)
            .filter(
                HistoricalPrice.symbol.in_(symbols),
                HistoricalPrice.date >= start_date,
                HistoricalPrice.date <= end_date,
            )
            .order_by(HistoricalPrice.date)
            .all()
        )

        for price in prices:
            data_by_symbol[price.symbol].append(
                {
                    "date": price.date,
                    "open": price.open,
                    "high": price.high,
                    "low": price.low,
                    "close": price.close,
                    "volume": price.volume,
                }
            )

        return data_by_symbol

    def get_daily_data_chunked(
        self, symbols: list[str], start_date: date, end_date: date, chunk_days: int = 30
    ):
        """Fetch data in chunks to avoid memory issues"""
        current_start = start_date
        while current_start <= end_date:
            current_end = min(current_start + timedelta(days=chunk_days), end_date)
            chunk_data = self.get_daily_data(symbols, current_start, current_end)
            yield chunk_data
            current_start = current_end + timedelta(days=1)

    def get_latest_price(self, symbol: str) -> float | None:
        """Get the latest price for a symbol"""
        from ..models.price import HistoricalPrice, IntradayCandle

        # Try intraday first
        candle = (
            self.db.query(IntradayCandle)
            .filter(IntradayCandle.symbol == symbol)
            .order_by(IntradayCandle.timestamp.desc())
            .first()
        )

        if candle:
            return candle.close

        # Fallback to daily
        daily = (
            self.db.query(HistoricalPrice)
            .filter(HistoricalPrice.symbol == symbol)
            .order_by(HistoricalPrice.date.desc())
            .first()
        )

        if daily:
            return daily.close

        return None

    def get_symbols_in_universe(self, universe_code: str, target_date: date) -> list[str]:
        """Get symbols in an index universe for a given date"""
        from ..models.universe import IndexConstituentHistory, IndexUniverseDefinition

        universe = (
            self.db.query(IndexUniverseDefinition)
            .filter(IndexUniverseDefinition.index_code == universe_code)
            .first()
        )

        if not universe:
            return []

        constituents = (
            self.db.query(IndexConstituentHistory)
            .filter(
                IndexConstituentHistory.universe_id == universe.id,
                IndexConstituentHistory.effective_from <= target_date,
            )
            .all()
        )

        # Get symbols that were active on the target date
        symbols = []
        for c in constituents:
            if c.effective_to is None or c.effective_to >= target_date:
                symbols.append(c.symbol)

        return symbols

    def close(self):
        """Close the database session"""
        if self.db:
            self.db.close()
