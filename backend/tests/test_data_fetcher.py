"""
Comprehensive tests for data_fetcher module
Tests historical data fetching, quotes, and error handling
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
import pandas as pd
from datetime import datetime, timedelta, date

from app.data_fetcher import (
    fetch_fyers_historical,
    fetch_historical_data,
    fetch_fyers_quotes,
    fetch_fyers_preopen,
    get_enhanced_quote
)


@pytest.fixture
def mock_fyers_client():
    """Create a mock Fyers client"""
    client = Mock()
    return client


@pytest.fixture
def sample_historical_response():
    """Sample Fyers historical data response"""
    return {
        's': 'ok',
        'candles': [
            [1640000000, 100.0, 105.0, 99.0, 103.0, 1000000],
            [1640086400, 103.0, 108.0, 102.0, 107.0, 1200000],
            [1640172800, 107.0, 110.0, 106.0, 109.0, 1100000],
        ]
    }


@pytest.fixture
def sample_quotes_response():
    """Sample Fyers quotes response"""
    return {
        's': 'ok',
        'd': [
            {
                'n': 'NSE:SBIN-EQ',
                'v': {
                    'lp': 500.50,
                    'volume': 10000,
                    'high_price': 505.0,
                    'low_price': 498.0,
                    'open_price': 499.0,
                    'prev_close_price': 497.0
                }
            },
            {
                'n': 'NSE:RELIANCE-EQ',
                'v': {
                    'lp': 2500.0,
                    'volume': 50000,
                    'high_price': 2520.0,
                    'low_price': 2490.0,
                    'open_price': 2495.0,
                    'prev_close_price': 2480.0
                }
            }
        ]
    }


class TestFetchFyersHistorical:
    """Tests for fetch_fyers_historical function"""

    @patch('app.data_fetcher.config.HAS_FYERS', True)
    @patch('app.data_fetcher.get_fyers_client')
    def test_fetch_historical_success(self, mock_get_client, sample_historical_response):
        """Test successful historical data fetch"""
        mock_client = Mock()
        mock_client.get_historical_data.return_value = sample_historical_response
        mock_get_client.return_value = mock_client

        df = fetch_fyers_historical('SBIN', days=365)

        assert df is not None
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3
        assert list(df.columns) == ['Open', 'High', 'Low', 'Close', 'Volume']
        assert df['Close'].iloc[0] == 103.0

    @patch('app.data_fetcher.config.HAS_FYERS', False)
    def test_fetch_historical_no_fyers(self):
        """Test when Fyers is not configured"""
        df = fetch_fyers_historical('SBIN', days=365)
        assert df is None

    @patch('app.data_fetcher.config.HAS_FYERS', True)
    @patch('app.data_fetcher.get_fyers_client')
    def test_fetch_historical_token_expired(self, mock_get_client):
        """Test token expiration handling"""
        mock_client = Mock()
        mock_client.get_historical_data.return_value = {
            'code': 401,
            's': 'error',
            'message': 'Token expired'
        }
        mock_get_client.return_value = mock_client

        df = fetch_fyers_historical('SBIN', days=365)
        assert df is None

    @patch('app.data_fetcher.config.HAS_FYERS', True)
    @patch('app.data_fetcher.get_fyers_client')
    def test_fetch_historical_api_error(self, mock_get_client):
        """Test API error handling"""
        mock_client = Mock()
        mock_client.get_historical_data.return_value = {
            's': 'error',
            'message': 'Invalid symbol'
        }
        mock_get_client.return_value = mock_client

        df = fetch_fyers_historical('INVALID', days=365)
        assert df is None

    @patch('app.data_fetcher.config.HAS_FYERS', True)
    @patch('app.data_fetcher.get_fyers_client')
    def test_fetch_historical_no_candles(self, mock_get_client):
        """Test when response has no candles"""
        mock_client = Mock()
        mock_client.get_historical_data.return_value = {
            's': 'ok'
        }
        mock_get_client.return_value = mock_client

        df = fetch_fyers_historical('SBIN', days=365)
        assert df is None

    @patch('app.data_fetcher.config.HAS_FYERS', True)
    @patch('app.data_fetcher.get_fyers_client')
    def test_fetch_historical_exception(self, mock_get_client):
        """Test exception handling"""
        mock_client = Mock()
        mock_client.get_historical_data.side_effect = Exception("Connection error")
        mock_get_client.return_value = mock_client

        df = fetch_fyers_historical('SBIN', days=365)
        assert df is None


class TestFetchFyersQuotes:
    """Tests for fetch_fyers_quotes function"""

    @patch('app.data_fetcher.config.HAS_FYERS', True)
    @patch('app.data_fetcher.get_fyers_client')
    def test_fetch_quotes_success(self, mock_get_client, sample_quotes_response):
        """Test successful quotes fetch"""
        mock_client = Mock()
        mock_client.get_quotes.return_value = sample_quotes_response
        mock_get_client.return_value = mock_client

        quotes = fetch_fyers_quotes(['SBIN', 'RELIANCE'])

        assert isinstance(quotes, dict)
        assert len(quotes) == 2
        assert 'SBIN' in quotes
        assert 'RELIANCE' in quotes
        assert quotes['SBIN']['ltp'] == 500.50
        assert quotes['RELIANCE']['ltp'] == 2500.0

    @patch('app.data_fetcher.config.HAS_FYERS', False)
    def test_fetch_quotes_no_fyers(self):
        """Test when Fyers is not configured"""
        quotes = fetch_fyers_quotes(['SBIN'])
        assert quotes == {}

    @patch('app.data_fetcher.config.HAS_FYERS', True)
    @patch('app.data_fetcher.get_fyers_client')
    def test_fetch_quotes_api_error(self, mock_get_client):
        """Test API error handling"""
        mock_client = Mock()
        mock_client.get_quotes.return_value = {
            's': 'error',
            'message': 'API Error'
        }
        mock_get_client.return_value = mock_client

        quotes = fetch_fyers_quotes(['SBIN'])
        assert quotes == {}

    @patch('app.data_fetcher.config.HAS_FYERS', True)
    @patch('app.data_fetcher.get_fyers_client')
    def test_fetch_quotes_exception(self, mock_get_client):
        """Test exception handling"""
        mock_client = Mock()
        mock_client.get_quotes.side_effect = Exception("Network error")
        mock_get_client.return_value = mock_client

        quotes = fetch_fyers_quotes(['SBIN'])
        assert quotes == {}


class TestFetchFyersPreopen:
    """Tests for fetch_fyers_preopen function"""

    @patch('app.data_fetcher.config.HAS_FYERS', False)
    def test_fetch_preopen_no_fyers(self):
        """Test when Fyers is not configured"""
        result = fetch_fyers_preopen('SBIN')
        assert result is None

    @patch('app.data_fetcher.config.HAS_FYERS', True)
    @patch('app.data_fetcher.get_fyers_client')
    def test_fetch_preopen_success(self, mock_get_client):
        """Test successful preopen data fetch"""
        mock_client = Mock()
        mock_client.get_parsed_quotes.return_value = {
            'SBIN': {
                'ltp': 500.0,
                'volume': 1000
            }
        }
        mock_get_client.return_value = mock_client

        result = fetch_fyers_preopen('SBIN')

        assert result is not None
        assert result['symbol'] == 'SBIN'
        assert result['price'] == 500.0
        assert result['volume'] == 1000
        assert result['source'] == 'fyers_preopen'

    @patch('app.data_fetcher.config.HAS_FYERS', True)
    @patch('app.data_fetcher.get_fyers_client')
    def test_fetch_preopen_no_data(self, mock_get_client):
        """Test when no preopen data available"""
        mock_client = Mock()
        mock_client.get_parsed_quotes.return_value = {}
        mock_get_client.return_value = mock_client

        result = fetch_fyers_preopen('SBIN')
        assert result is None

    @patch('app.data_fetcher.config.HAS_FYERS', True)
    @patch('app.data_fetcher.get_fyers_client')
    def test_fetch_preopen_exception(self, mock_get_client):
        """Test exception handling"""
        mock_client = Mock()
        mock_client.get_parsed_quotes.side_effect = Exception("Error")
        mock_get_client.return_value = mock_client

        result = fetch_fyers_preopen('SBIN')
        assert result is None


class TestGetEnhancedQuote:
    """Tests for get_enhanced_quote function"""

    def test_enhanced_quote_with_hist_data(self):
        """Test enhanced quote with historical data only"""
        hist_data = pd.DataFrame({
            'close': [100.0, 101.0, 102.0],
            'volume': [1000, 1100, 1200],
            'high': [105.0, 106.0, 107.0],
            'low': [99.0, 100.0, 101.0]
        })

        result = get_enhanced_quote('SBIN', hist_data)

        assert result['symbol'] == 'SBIN'
        assert result['source'] == 'database'
        assert result['close'] == 102.0
        assert result['volume'] == 1200

    def test_enhanced_quote_empty_hist_data(self):
        """Test enhanced quote with empty historical data"""
        hist_data = pd.DataFrame()

        result = get_enhanced_quote('SBIN', hist_data)

        assert result['symbol'] == 'SBIN'
        assert result['source'] == 'database'
        assert 'close' not in result

    def test_enhanced_quote_none_hist_data(self):
        """Test enhanced quote with None historical data"""
        result = get_enhanced_quote('SBIN', None)

        assert result['symbol'] == 'SBIN'
        assert result['source'] == 'database'

    @patch('app.data_fetcher.config.HAS_FYERS', True)
    @patch('app.data_fetcher.fetch_fyers_quotes')
    def test_enhanced_quote_with_fyers(self, mock_fetch_quotes):
        """Test enhanced quote with Fyers real-time data"""
        hist_data = pd.DataFrame({
            'close': [100.0],
            'volume': [1000],
            'high': [105.0],
            'low': [99.0]
        })

        mock_fetch_quotes.return_value = {
            'SBIN': {
                'ltp': 103.0,
                'volume': 1500,
                'high': 108.0,
                'low': 102.0
            }
        }

        result = get_enhanced_quote('SBIN', hist_data)

        assert result['symbol'] == 'SBIN'
        assert result['source'] == 'fyers'
        assert result['close'] == 103.0
        assert result['volume'] == 1500


class TestFetchHistoricalData:
    """Tests for fetch_historical_data with database integration"""

    @patch('app.data_fetcher.SessionLocal')
    @patch('app.data_fetcher.DataRepository')
    def test_fetch_with_no_db_data(self, mock_repo_class, mock_session):
        """Test fetch when no data in database"""
        mock_db = Mock()
        mock_session.return_value = mock_db

        mock_repo = Mock()
        mock_repo.get_latest_price_date.return_value = None
        mock_repo_class.return_value = mock_repo

        with patch('app.data_fetcher.config.HAS_FYERS', False):
            result = fetch_historical_data('SBIN', days=365)
            assert result is None

    @patch('app.data_fetcher.SessionLocal')
    @patch('app.data_fetcher.DataRepository')
    def test_fetch_with_recent_db_data(self, mock_repo_class, mock_session):
        """Test fetch when recent data exists in database"""
        mock_db = Mock()
        mock_session.return_value = mock_db

        mock_repo = Mock()
        mock_repo.get_latest_price_date.return_value = date.today()

        sample_df = pd.DataFrame({
            'open': [100.0],
            'high': [105.0],
            'low': [99.0],
            'close': [103.0],
            'volume': [1000]
        })
        mock_repo.get_historical_prices.return_value = sample_df
        mock_repo_class.return_value = mock_repo

        result = fetch_historical_data('SBIN', days=30)

        assert result is not None
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 1