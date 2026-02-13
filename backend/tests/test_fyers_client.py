"""
Comprehensive tests for Fyers client
Tests singleton pattern, API methods, and error handling
"""
import pytest
from unittest.mock import Mock, patch, MagicMock, mock_open
import json

from app.services.fyers_client import FyersClient, get_fyers_client


class TestFyersClientSingleton:
    """Tests for Fyers client singleton pattern"""

    def test_singleton_instance(self):
        """Test that FyersClient is a singleton"""
        client1 = get_fyers_client()
        client2 = get_fyers_client()

        assert client1 is client2

    @patch('app.services.fyers_client.os.path.exists', return_value=False)
    def test_client_without_credentials(self, mock_exists):
        """Test client initialization without credentials file"""
        # Create new instance (clear singleton)
        FyersClient._instance = None

        with patch('builtins.open', mock_open(read_data='{}')):
            client = FyersClient()

            assert client.fyers is None
            assert client.access_token is None


class TestCredentialsLoading:
    """Tests for credential loading"""

    @patch('builtins.open', new_callable=mock_open, read_data='{"client_id": "test_client", "access_token": "test_token"}')
    @patch('app.services.fyers_client.os.path.exists', return_value=True)
    def test_load_valid_credentials(self, mock_exists, mock_file):
        """Test loading valid credentials"""
        FyersClient._instance = None
        client = FyersClient()

        assert client.client_id == 'test_client'
        assert client.access_token == 'test_token'

    @patch('builtins.open', side_effect=Exception("File read error"))
    @patch('app.services.fyers_client.os.path.exists', return_value=True)
    def test_load_credentials_error(self, mock_exists, mock_file):
        """Test credential loading error handling"""
        FyersClient._instance = None
        client = FyersClient()

        assert client.fyers is None


class TestTokenValidation:
    """Tests for token validation"""

    def test_validate_token_success(self):
        """Test successful token validation"""
        client = FyersClient()
        client.fyers = Mock()
        client.fyers.get_profile.return_value = {'s': 'ok'}

        assert client.validate_token() is True

    def test_validate_token_failure(self):
        """Test failed token validation"""
        client = FyersClient()
        client.fyers = Mock()
        client.fyers.get_profile.return_value = {'s': 'error'}

        assert client.validate_token() is False

    def test_validate_token_no_client(self):
        """Test token validation without client"""
        client = FyersClient()
        client.fyers = None

        assert client.validate_token() is False

    def test_validate_token_exception(self):
        """Test token validation with exception"""
        client = FyersClient()
        client.fyers = Mock()
        client.fyers.get_profile.side_effect = Exception("Network error")

        assert client.validate_token() is False


class TestGetQuotes:
    """Tests for get_quotes method"""

    def test_get_quotes_success(self):
        """Test successful quotes fetch"""
        client = FyersClient()
        client.fyers = Mock()
        client.fyers.quotes.return_value = {
            's': 'ok',
            'd': [
                {
                    'n': 'NSE:SBIN-EQ',
                    'v': {'lp': 500.0, 'volume': 10000}
                }
            ]
        }

        result = client.get_quotes(['NSE:SBIN-EQ'])

        assert result['s'] == 'ok'
        assert len(result['d']) == 1

    def test_get_quotes_no_client(self):
        """Test quotes fetch without client"""
        client = FyersClient()
        client.fyers = None

        result = client.get_quotes(['NSE:SBIN-EQ'])

        assert result == {}

    def test_get_quotes_exception(self):
        """Test quotes fetch with exception"""
        client = FyersClient()
        client.fyers = Mock()
        client.fyers.quotes.side_effect = Exception("API error")

        result = client.get_quotes(['NSE:SBIN-EQ'])

        assert result == {}


class TestGetParsedQuotes:
    """Tests for get_parsed_quotes method"""

    def test_get_parsed_quotes_success(self):
        """Test successful parsed quotes"""
        client = FyersClient()
        client.fyers = Mock()
        client.fyers.quotes.return_value = {
            's': 'ok',
            'd': [
                {
                    'n': 'NSE:SBIN-EQ',
                    'v': {
                        'lp': 500.0,
                        'volume': 10000,
                        'high_price': 505.0,
                        'low_price': 498.0,
                        'open_price': 499.0,
                        'prev_close_price': 497.0
                    }
                }
            ]
        }

        result = client.get_parsed_quotes(['NSE:SBIN-EQ'])

        assert 'SBIN' in result
        assert result['SBIN']['ltp'] == 500.0
        assert result['SBIN']['change_pct'] == pytest.approx((500.0 - 497.0) / 497.0 * 100, rel=0.01)

    def test_get_parsed_quotes_index(self):
        """Test parsing index quotes"""
        client = FyersClient()
        client.fyers = Mock()
        client.fyers.quotes.return_value = {
            's': 'ok',
            'd': [
                {
                    'n': 'NSE:NIFTY50-INDEX',
                    'v': {
                        'lp': 18000.0,
                        'volume': 0,
                        'high_price': 18100.0,
                        'low_price': 17950.0,
                        'open_price': 18000.0,
                        'prev_close_price': 17980.0
                    }
                }
            ]
        }

        result = client.get_parsed_quotes(['NSE:NIFTY50-INDEX'])

        assert 'NIFTY50' in result
        assert result['NIFTY50']['ltp'] == 18000.0

    def test_get_parsed_quotes_error_response(self):
        """Test parsing quotes with error response"""
        client = FyersClient()
        client.fyers = Mock()
        client.fyers.quotes.return_value = {'s': 'error'}

        result = client.get_parsed_quotes(['NSE:SBIN-EQ'])

        assert result == {}

    def test_get_parsed_quotes_zero_prev_close(self):
        """Test change percentage calculation with zero prev_close"""
        client = FyersClient()
        client.fyers = Mock()
        client.fyers.quotes.return_value = {
            's': 'ok',
            'd': [
                {
                    'n': 'NSE:TEST-EQ',
                    'v': {
                        'lp': 100.0,
                        'prev_close_price': 0
                    }
                }
            ]
        }

        result = client.get_parsed_quotes(['NSE:TEST-EQ'])

        assert result['TEST']['change_pct'] == 0


class TestGetHistoricalData:
    """Tests for get_historical_data method"""

    def test_get_historical_data_success(self):
        """Test successful historical data fetch"""
        client = FyersClient()
        client.fyers = Mock()
        client.fyers.history.return_value = {
            's': 'ok',
            'candles': [
                [1640000000, 100.0, 105.0, 99.0, 103.0, 1000000]
            ]
        }

        result = client.get_historical_data(
            symbol='NSE:SBIN-EQ',
            timeframe='D',
            range_from='2024-01-01',
            range_to='2024-01-31'
        )

        assert result['s'] == 'ok'
        assert 'candles' in result

    def test_get_historical_data_no_client(self):
        """Test historical data fetch without client"""
        client = FyersClient()
        client.fyers = None

        result = client.get_historical_data(
            symbol='NSE:SBIN-EQ',
            timeframe='D',
            range_from='2024-01-01',
            range_to='2024-01-31'
        )

        assert result == {}

    def test_get_historical_data_exception(self):
        """Test historical data fetch with exception"""
        client = FyersClient()
        client.fyers = Mock()
        client.fyers.history.side_effect = Exception("API error")

        result = client.get_historical_data(
            symbol='NSE:SBIN-EQ',
            timeframe='D',
            range_from='2024-01-01',
            range_to='2024-01-31'
        )

        assert result == {}


class TestOrderMethods:
    """Tests for order-related methods"""

    def test_get_orderbook_success(self):
        """Test getting orderbook"""
        client = FyersClient()
        client.fyers = Mock()
        client.fyers.orderbook.return_value = {
            's': 'ok',
            'orderBook': []
        }

        result = client.get_orderbook()

        assert result['s'] == 'ok'
        assert 'orderBook' in result

    def test_get_orderbook_no_client(self):
        """Test orderbook without client"""
        client = FyersClient()
        client.fyers = None

        result = client.get_orderbook()

        assert result == {}

    def test_get_positions_success(self):
        """Test getting positions"""
        client = FyersClient()
        client.fyers = Mock()
        client.fyers.positions.return_value = {
            's': 'ok',
            'netPositions': []
        }

        result = client.get_positions()

        assert result['s'] == 'ok'

    def test_place_order_success(self):
        """Test placing order"""
        client = FyersClient()
        client.fyers = Mock()
        client.fyers.place_order.return_value = {
            's': 'ok',
            'id': 'order_123'
        }

        order_data = {
            'symbol': 'NSE:SBIN-EQ',
            'qty': 10,
            'type': 2,
            'side': 1
        }

        result = client.place_order(order_data)

        assert result['s'] == 'ok'
        assert 'id' in result

    def test_place_order_no_client(self):
        """Test placing order without client"""
        client = FyersClient()
        client.fyers = None

        result = client.place_order({})

        assert result['s'] == 'error'
        assert 'Client not connected' in result['message']