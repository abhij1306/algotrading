"""
Daily Update Master Script
=========================
Fetches latest prices from Fyers API and updates technical indicators.

Usage:
    python scripts/daily_update_master.py

Runs after market close (4:00 PM IST) or can be run manually.
"""
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

from app.database import SessionLocal
from app.indicators import atr, ema, rsi
from app.models import Company, HistoricalPrice
from app.services.fyers_client import get_fyers_client
from app.services.symbol_master import symbol_master

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class DailyUpdateMaster:
    """
    Orchestrates daily stock price and indicator updates.

    Workflow:
        1. Get all active stocks from database
        2. Fetch latest quotes from Fyers API (batch of 50)
        3. Update/create today's price record
        4. Recalculate technical indicators
    """

    # Fyers API batch size limit
    BATCH_SIZE = 50

    # How many days of history to fetch for indicator calculation
    HISTORY_DAYS = 60

    def __init__(self, dry_run: bool = False):
        """
        Initialize the daily updater.

        Args:
            dry_run: If True, don't commit changes to database
        """
        self.dry_run = dry_run
        self.fyers_client = None
        self.stats = {
            'total': 0,
            'success': 0,
            'failed': 0,
            'skipped': 0,
            'errors': []
        }

    def run(self):
        """Main entry point - run the daily update."""
        logger.info("=" * 60)
        logger.info(f"Daily Update Master - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)

        # Initialize Fyers client
        self._init_fyers_client()

        # Get all active stocks
        stocks = self._get_active_stocks()
        self.stats['total'] = len(stocks)
        logger.info(f"Found {len(stocks)} active stocks to update")

        # Process in batches
        for i in range(0, len(stocks), self.BATCH_SIZE):
            batch = stocks[i:i + self.BATCH_SIZE]
            logger.info(f"Processing batch {i // self.BATCH_SIZE + 1}: {len(batch)} stocks")

            self._process_batch(batch)

        # Print summary
        self._print_summary()

        return self.stats

    def _init_fyers_client(self):
        """Initialize Fyers client."""
        try:
            self.fyers_client = get_fyers_client()
            if self.fyers_client:
                logger.info("Fyers client initialized successfully")
            else:
                logger.warning("Fyers client not available - using fallback mode")
        except Exception as e:
            logger.error(f"Failed to initialize Fyers client: {e}")
            self.fyers_client = None

    def _get_active_stocks(self) -> list[Company]:
        """Get all active stocks from database."""
        db = SessionLocal()
        try:
            stocks = db.query(Company).filter(
                Company.is_active.is_(True)
            ).all()
            return stocks
        finally:
            db.close()

    def _process_batch(self, stocks: list[Company]):
        """Process a batch of stocks."""
        # Convert to Fyers format
        symbols = [s.symbol for s in stocks]
        fyers_symbols = symbol_master.batch_to_fyers(symbols)

        # Fetch quotes from Fyers
        quotes = self._fetch_quotes(fyers_symbols)

        # Update each stock
        for stock in stocks:
            try:
                fyers_symbol = symbol_master.to_fyers(stock.symbol)
                quote = quotes.get(fyers_symbol)

                if quote:
                    self._update_stock_price(stock, quote)
                    self.stats['success'] += 1
                else:
                    logger.warning(f"No quote for {stock.symbol}")
                    self.stats['skipped'] += 1

            except Exception as e:
                logger.error(f"Failed to update {stock.symbol}: {e}")
                self.stats['failed'] += 1
                self.stats['errors'].append(f"{stock.symbol}: {str(e)}")

    def _fetch_quotes(self, symbols: list[str]) -> dict[str, Any]:
        """Fetch quotes from Fyers API."""
        if not self.fyers_client:
            logger.warning("Fyers client not available, returning empty quotes")
            return {}

        try:
            result = self.fyers_client.get_quotes(symbols)

            # Parse the response
            quotes = {}
            if result and 'd' in result:
                for symbol, data in result['d'].items():
                    quotes[symbol] = {
                        'open': data.get('op', 0),
                        'high': data.get('h', 0),
                        'low': data.get('l', 0),
                        'close': data.get('lc', data.get('c', 0)),  # last close or current
                        'volume': data.get('v', 0),
                    }

            return quotes

        except Exception as e:
            logger.error(f"Failed to fetch quotes: {e}")
            return {}

    def _update_stock_price(self, stock: Company, quote: dict[str, Any]):
        """Update or create today's price record for a stock."""
        db = SessionLocal()
        try:
            today = date.today()

            # Check if today's record exists
            price_record = db.query(HistoricalPrice).filter(
                HistoricalPrice.company_id == stock.id,
                HistoricalPrice.date == today
            ).first()

            if price_record:
                # Update existing record
                price_record.open = quote.get('open', price_record.open)
                price_record.high = quote.get('high', price_record.high)
                price_record.low = quote.get('low', price_record.low)
                price_record.close = quote.get('close', price_record.close)
                price_record.volume = quote.get('volume', price_record.volume)
                logger.debug(f"Updated {stock.symbol}: {quote.get('close')}")
            else:
                # Create new record
                price_record = HistoricalPrice(
                    company_id=stock.id,
                    date=today,
                    open=quote.get('open', 0),
                    high=quote.get('high', 0),
                    low=quote.get('low', 0),
                    close=quote.get('close', 0),
                    volume=quote.get('volume', 0),
                    source='fyers'
                )
                db.add(price_record)
                logger.debug(f"Created {stock.symbol}: {quote.get('close')}")

            # Recalculate indicators for this stock
            self._recalculate_indicators(stock, db)

            if not self.dry_run:
                db.commit()

        except Exception as e:
            db.rollback()
            raise e
        finally:
            db.close()

    def _recalculate_indicators(self, stock: Company, db):
        """Recalculate technical indicators for a stock."""
        try:
            # Get last 60 days of data
            cutoff_date = date.today() - timedelta(days=self.HISTORY_DAYS)

            prices = db.query(HistoricalPrice).filter(
                HistoricalPrice.company_id == stock.id,
                HistoricalPrice.date >= cutoff_date
            ).order_by(HistoricalPrice.date.asc()).all()

            if len(prices) < 20:
                logger.debug(f"Not enough data for {stock.symbol} indicators")
                return

            # Convert to DataFrame
            df = pd.DataFrame([{
                'date': p.date,
                'open': p.open,
                'high': p.high,
                'low': p.low,
                'close': p.close,
                'volume': p.volume
            } for p in prices])

            # Calculate indicators
            df['ema_20'] = ema(df['close'], 20)
            df['ema_50'] = ema(df['close'], 50)
            df['rsi_14'] = rsi(df['close'], 14)
            df['atr_14'] = atr(df, 14)

            # Calculate additional indicators
            # MACD
            ema_12 = ema(df['close'], 12)
            ema_26 = ema(df['close'], 26)
            df['macd'] = ema_12 - ema_26
            df['macd_signal'] = ema(df['macd'], 9)
            df['macd_histogram'] = df['macd'] - df['macd_signal']

            # Bollinger Bands
            df['bb_middle'] = df['close'].rolling(20).mean()
            std = df['close'].rolling(20).std()
            df['bb_upper'] = df['bb_middle'] + (std * 2)
            df['bb_lower'] = df['bb_middle'] - (std * 2)

            # Stochastic
            low_14 = df['low'].rolling(14).min()
            high_14 = df['high'].rolling(14).max()
            df['stoch_k'] = 100 * ((df['close'] - low_14) / (high_14 - low_14 + 1e-9))
            df['stoch_d'] = df['stoch_k'].rolling(3).mean()

            # Update the latest record with indicators
            latest = df.iloc[-1]
            latest_price = db.query(HistoricalPrice).filter(
                HistoricalPrice.company_id == stock.id,
                HistoricalPrice.date == date.today()
            ).first()

            if latest_price:
                latest_price.ema_20 = float(latest['ema_20']) if pd.notna(latest['ema_20']) else None
                latest_price.ema_50 = float(latest['ema_50']) if pd.notna(latest['ema_50']) else None
                latest_price.rsi_14 = float(latest['rsi_14']) if pd.notna(latest['rsi_14']) else None
                latest_price.atr_14 = float(latest['atr_14']) if pd.notna(latest['atr_14']) else None
                latest_price.macd = float(latest['macd']) if pd.notna(latest['macd']) else None
                latest_price.macd_signal = float(latest['macd_signal']) if pd.notna(latest['macd_signal']) else None
                latest_price.macd_histogram = float(latest['macd_histogram']) if pd.notna(latest['macd_histogram']) else None
                latest_price.bb_upper = float(latest['bb_upper']) if pd.notna(latest['bb_upper']) else None
                latest_price.bb_middle = float(latest['bb_middle']) if pd.notna(latest['bb_middle']) else None
                latest_price.bb_lower = float(latest['bb_lower']) if pd.notna(latest['bb_lower']) else None
                latest_price.stoch_k = float(latest['stoch_k']) if pd.notna(latest['stoch_k']) else None
                latest_price.stoch_d = float(latest['stoch_d']) if pd.notna(latest['stoch_d']) else None

                # Trend indicators
                if len(df) >= 7:
                    latest_price.trend_7d = ((df['close'].iloc[-1] - df['close'].iloc[-7]) / df['close'].iloc[-7] * 100) if len(df) >= 7 else None
                if len(df) >= 30:
                    latest_price.trend_30d = ((df['close'].iloc[-1] - df['close'].iloc[-30]) / df['close'].iloc[-30] * 100) if len(df) >= 30 else None

                # 20-day high
                if len(df) >= 20:
                    latest_price.high_20d = float(df['high'].iloc[-20:].max())
                    latest_price.is_breakout = df['close'].iloc[-1] >= df['high'].iloc[-20:].max() if len(df) >= 20 else False

        except Exception as e:
            logger.debug(f"Indicator calculation error for {stock.symbol}: {e}")

    def _print_summary(self):
        """Print update summary."""
        logger.info("=" * 60)
        logger.info("UPDATE SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Total stocks:     {self.stats['total']}")
        logger.info(f"Success:         {self.stats['success']}")
        logger.info(f"Failed:          {self.stats['failed']}")
        logger.info(f"Skipped:         {self.stats['skipped']}")

        if self.stats['errors']:
            logger.info(f"\nErrors ({len(self.stats['errors'])}):")
            for error in self.stats['errors'][:10]:  # Show first 10
                logger.info(f"  - {error}")

        logger.info("=" * 60)


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description='Daily stock price update')
    parser.add_argument('--dry-run', action='store_true', help='Run without saving to database')
    args = parser.parse_args()

    updater = DailyUpdateMaster(dry_run=args.dry_run)
    stats = updater.run()

    # Exit with error code if any failures
    sys.exit(0 if stats['failed'] == 0 else 1)


if __name__ == '__main__':
    main()
