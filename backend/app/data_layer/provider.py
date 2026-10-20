"""
Unified Data Provider
Single point of access for all market data (Historical, Fundamental, Live Quotes)

Architecture:
- PostgreSQL: Metadata, Financial Statements, Historical Prices
- Parquet: Heavy time-series data (future optimization)
- Fyers API v3: Live quotes only
"""

import logging
from datetime import datetime

import pandas as pd
from sqlalchemy.orm import Session

from ..data_repository import DataRepository
from ..database import Company, FinancialStatement, HistoricalPrice
from ..utils.db_errors import is_db_unavailable
from ..utils.logger import log_warning_throttled
from .exceptions import (
    DataNotFoundError,
    DataSourceUnavailableError,
    InvalidSymbolError,
    MissingTokenError,
)

logger = logging.getLogger(__name__)

# Intraday timeframes accepted by get_history, mapped to IntradayCandle.timeframe minutes.
INTRADAY_TIMEFRAME_MINUTES = {"1MIN": 1, "5MIN": 5, "15MIN": 15, "30MIN": 30, "1H": 60}


class DataProvider:
    """
    Unified data access layer enforcing:
    1. Single Source of Truth (PostgreSQL/Parquet)
    2. Fyers for live quotes only
    3. Standard exception handling
    4. No direct Fyers access from UI components
    """

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = DataRepository(db)

    def get_history(
        self,
        symbol: str,
        timeframe: str = "1D",
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        days: int = 365,
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV data

        Args:
            symbol: Stock symbol (e.g., 'RELIANCE', 'NIFTY50')
            timeframe: '1D' (daily) or an intraday key of INTRADAY_TIMEFRAME_MINUTES
            start_date: Start date (defaults to `days` ago)
            end_date: End date (defaults to today)
            days: Lookback days if start_date not provided

        Returns:
            Daily: the repository's daily OHLCV frame.
            Intraday: columns [timestamp, open, high, low, close, volume].

        Raises:
            DataNotFoundError: If symbol has no data
            InvalidSymbolError: If symbol doesn't exist
        """
        # Validate symbol
        company = self.db.query(Company).filter(Company.symbol == symbol.upper()).first()
        if not company:
            raise InvalidSymbolError(symbol)

        effective_days = days if (start_date is None and end_date is None) else None

        # Daily data: Use PostgreSQL
        if timeframe == "1D":
            df = self.repo.get_historical_prices(
                symbol,
                days=effective_days,
                start_date=start_date.date() if start_date else None,
                end_date=end_date.date() if end_date else None,
            )
            if df is None or df.empty:
                raise DataNotFoundError(symbol, f"No historical data available for {symbol}")

            return df

        # Intraday data: IntradayCandle table (with the repository's CSV fallback)
        minutes = INTRADAY_TIMEFRAME_MINUTES.get(timeframe.upper())
        if minutes is None:
            raise NotImplementedError(
                f"Timeframe '{timeframe}' not supported. "
                f"Use '1D' or one of: {', '.join(INTRADAY_TIMEFRAME_MINUTES)}."
            )
        df = self.repo.get_intraday_candles(
            company.symbol,
            timeframe=minutes,
            start_date=start_date,
            end_date=end_date,
            days=effective_days,
        )
        if df is None or df.empty:
            raise DataNotFoundError(symbol, f"No {timeframe} intraday data available for {symbol}")
        return df

    def get_fundamentals(self, symbol: str) -> dict:
        """
        Fetch latest financial statement

        Returns:
            Dict with keys: revenue, net_income, eps, pe_ratio, debt_to_equity, roe, etc.

        Raises:
            DataNotFoundError: If no financial data exists
        """
        company = self.db.query(Company).filter(Company.symbol == symbol.upper()).first()
        if not company:
            raise InvalidSymbolError(symbol)

        fs = (
            self.db.query(FinancialStatement)
            .filter(FinancialStatement.company_id == company.id)
            .order_by(FinancialStatement.period_end.desc())
            .first()
        )

        if not fs:
            raise DataNotFoundError(symbol, f"No financial statements available for {symbol}")

        return {
            "symbol": symbol,
            "period_end": fs.period_end.isoformat() if fs.period_end else None,
            "revenue": fs.revenue,
            "net_income": fs.net_income,
            "eps": fs.eps,
            "pe_ratio": fs.pe_ratio,
            "pb_ratio": fs.pb_ratio,
            "debt_to_equity": fs.debt_to_equity,
            "roe": fs.roe,
            "roa": fs.roa,
            "market_cap": company.market_cap,
        }

    def get_quote(self, symbol: str) -> dict:
        """
        Fetch live quote from Fyers

        Returns:
            Dict with keys: ltp, open, high, low, close, volume, change_pct

        Raises:
            DataSourceUnavailableError: If Fyers is down
        """
        try:
            from ..data_fetcher import fetch_fyers_quotes

            quotes = fetch_fyers_quotes([symbol])
            if not quotes or symbol not in quotes:
                raise DataNotFoundError(symbol, f"No live quote available for {symbol}")

            return quotes[symbol]

        except Exception as e:
            if "token" in str(e).lower():
                raise MissingTokenError() from e
            raise DataSourceUnavailableError("Fyers", str(e)) from e

    def get_multiple_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """
        Batch fetch live quotes

        Returns:
            Dict mapping symbol -> quote data
        """
        try:
            from ..data_fetcher import fetch_fyers_quotes

            return fetch_fyers_quotes(symbols)
        except Exception as e:
            if "token" in str(e).lower():
                raise MissingTokenError() from e
            raise DataSourceUnavailableError("Fyers", str(e)) from e

    def health_check(self) -> dict:
        """
        Verify data source availability

        Returns:
            {
                "postgresql": "OK" | "DOWN",
                "fyers": "OK" | "DOWN" | "TOKEN_EXPIRED",
                "last_update": ISO timestamp,
                "total_symbols": int
            }
        """
        status = {
            "postgresql": "UNKNOWN",
            "fyers": "UNKNOWN",
            "last_update": None,
            "total_symbols": 0,
        }

        # Check PostgreSQL
        # Driver and broker error text goes to the server log only, never into the reply.
        try:
            count = self.db.query(Company).count()
            status["postgresql"] = "OK"
            status["total_symbols"] = count
        except Exception as e:
            self.db.rollback()
            status["postgresql"] = "DOWN"
            if is_db_unavailable(e):
                log_warning_throttled(
                    logger, "health.postgresql", "Health check: database unreachable"
                )
            else:
                logger.exception("Health check: database query failed")

        # Check Fyers
        try:
            from ..services.fyers_client import get_fyers_client

            client = get_fyers_client()
            client.fyers.get_profile()
            status["fyers"] = "OK"
        except Exception as e:
            if "token" in str(e).lower() or "401" in str(e):
                status["fyers"] = "TOKEN_EXPIRED"
            else:
                status["fyers"] = "DOWN"
                logger.warning("Health check: Fyers unavailable (%s)", type(e).__name__)

        if status["postgresql"] != "OK":
            return status

        # Get latest data update timestamp
        try:
            latest = (
                self.db.query(HistoricalPrice).order_by(HistoricalPrice.created_at.desc()).first()
            )
            if latest:
                status["last_update"] = latest.created_at.isoformat()
        except Exception:
            logger.debug("Could not read latest data update timestamp", exc_info=True)

        return status
