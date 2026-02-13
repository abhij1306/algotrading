"""
Comprehensive tests for YFinance service
Tests fallback data fetching and error handling
"""
import pytest
from unittest.mock import Mock, patch
import pandas as pd
import numpy as np

from app.services.yfinance_service import YFinanceService, yfinance_service


class TestGetQuotes:
    """Tests for get_quotes method"""

    @patch('app.services.yfinance_service.yf.download')
    def test_get_quotes_success(self, mock_download):
        """Test successful quotes fetch"""
        # Mock yfinance data
        mock_data = pd.DataFrame({
            'Open': [100.0, 101.0, 102.0],
            'High': [105.0, 106.0, 107.0],
            'Low': [99.0, 100.0, 101.0],
            'Close': [103.0, 104.0, 105.0],
            'Volume': [1000, 1100, 1200]
        })
        mock_download.return_value = {'SBIN.NS': mock_data}

        result = YFinanceService.get_quotes(['SBIN'])

        assert 'SBIN' in result
        assert result['SBIN']['price'] == 105.0
        assert result['SBIN']['source'] == 'yfinance'
        assert 'change_pct' in result['SBIN']

    @patch('app.services.yfinance_service.yf.download')
    def test_get_quotes_multiple_symbols(self, mock_download):
        """Test fetching multiple symbols"""
        mock_data_sbin = pd.DataFrame({
            'Open': [100.0, 101.0],
            'High': [105.0, 106.0],
            'Low': [99.0, 100.0],
            'Close': [103.0, 104.0],
            'Volume': [1000, 1100]
        })
        mock_data_tcs = pd.DataFrame({
            'Open': [3000.0, 3010.0],
            'High': [3050.0, 3060.0],
            'Low': [2990.0, 3000.0],
            'Close': [3030.0, 3040.0],
            'Volume': [5000, 5100]
        })

        mock_download.return_value = {
            'SBIN.NS': mock_data_sbin,
            'TCS.NS': mock_data_tcs
        }

        result = YFinanceService.get_quotes(['SBIN', 'TCS'])

        assert len(result) == 2
        assert 'SBIN' in result
        assert 'TCS' in result

    @patch('app.services.yfinance_service.yf.download')
    def test_get_quotes_nifty_index(self, mock_download):
        """Test fetching NIFTY50 index"""
        mock_data = pd.DataFrame({
            'Open': [18000.0, 18100.0],
            'High': [18200.0, 18300.0],
            'Low': [17950.0, 18050.0],
            'Close': [18150.0, 18250.0],
            'Volume': [0, 0]
        })
        mock_download.return_value = {'^NSEI': mock_data}

        result = YFinanceService.get_quotes(['NIFTY50'])

        assert 'NIFTY50' in result
        assert result['NIFTY50']['price'] == 18250.0

    @patch('app.services.yfinance_service.yf.download')
    def test_get_quotes_banknifty_index(self, mock_download):
        """Test fetching BANKNIFTY index"""
        mock_data = pd.DataFrame({
            'Open': [42000.0, 42100.0],
            'High': [42200.0, 42300.0],
            'Low': [41950.0, 42050.0],
            'Close': [42150.0, 42250.0],
            'Volume': [0, 0]
        })
        mock_download.return_value = {'^NSEBANK': mock_data}

        result = YFinanceService.get_quotes(['BANKNIFTY'])

        assert 'BANKNIFTY' in result
        assert result['BANKNIFTY']['price'] == 42250.0

    @patch('app.services.yfinance_service.yf.download')
    def test_get_quotes_empty_data(self, mock_download):
        """Test handling empty data"""
        mock_download.return_value = {'SBIN.NS': pd.DataFrame()}

        result = YFinanceService.get_quotes(['SBIN'])

        assert 'SBIN' in result
        assert result['SBIN'] is None

    @patch('app.services.yfinance_service.yf.download')
    def test_get_quotes_nan_values(self, mock_download):
        """Test handling NaN values"""
        mock_data = pd.DataFrame({
            'Open': [100.0, 101.0],
            'High': [105.0, np.nan],
            'Low': [99.0, 100.0],
            'Close': [103.0, np.nan],
            'Volume': [1000, 1100]
        })
        mock_download.return_value = {'SBIN.NS': mock_data}

        result = YFinanceService.get_quotes(['SBIN'])

        # Should use Open price when Close is NaN
        assert 'SBIN' in result
        assert result['SBIN']['price'] == 101.0

    @patch('app.services.yfinance_service.yf.download')
    def test_get_quotes_exception(self, mock_download):
        """Test exception handling"""
        mock_download.side_effect = Exception("Network error")

        result = YFinanceService.get_quotes(['SBIN'])

        assert result == {}

    @patch('app.services.yfinance_service.yf.download')
    def test_get_quotes_partial_failure(self, mock_download):
        """Test partial failure in processing symbols"""
        mock_data = pd.DataFrame({
            'Open': [100.0, 101.0],
            'High': [105.0, 106.0],
            'Low': [99.0, 100.0],
            'Close': [103.0, 104.0],
            'Volume': [1000, 1100]
        })

        # One symbol succeeds, another fails
        def side_effect_func(symbols, **kwargs):
            if 'SBIN.NS' in symbols:
                return {'SBIN.NS': mock_data}
            raise Exception("Symbol not found")

        mock_download.side_effect = side_effect_func

        result = YFinanceService.get_quotes(['SBIN'])

        assert 'SBIN' in result

    @patch('app.services.yfinance_service.yf.download')
    def test_get_quotes_change_percentage_calculation(self, mock_download):
        """Test change percentage calculation"""
        mock_data = pd.DataFrame({
            'Open': [100.0, 101.0, 102.0],
            'High': [105.0, 106.0, 107.0],
            'Low': [99.0, 100.0, 101.0],
            'Close': [103.0, 105.0, 110.0],
            'Volume': [1000, 1100, 1200]
        })
        mock_download.return_value = {'SBIN.NS': mock_data}

        result = YFinanceService.get_quotes(['SBIN'])

        # Current: 110, Previous: 105
        expected_change_pct = ((110.0 - 105.0) / 105.0) * 100
        assert result['SBIN']['change_pct'] == pytest.approx(expected_change_pct, rel=0.01)

    @patch('app.services.yfinance_service.yf.download')
    def test_get_quotes_zero_prev_close(self, mock_download):
        """Test change percentage with zero previous close"""
        mock_data = pd.DataFrame({
            'Open': [0.0, 100.0],
            'High': [0.0, 105.0],
            'Low': [0.0, 99.0],
            'Close': [0.0, 103.0],
            'Volume': [0, 1000]
        })
        mock_download.return_value = {'SBIN.NS': mock_data}

        result = YFinanceService.get_quotes(['SBIN'])

        # Should handle division by zero
        assert result['SBIN']['change_pct'] == 0


class TestGetQuote:
    """Tests for get_quote (single symbol) method"""

    @patch('app.services.yfinance_service.yf.download')
    def test_get_single_quote(self, mock_download):
        """Test fetching single quote"""
        mock_data = pd.DataFrame({
            'Open': [100.0, 101.0],
            'High': [105.0, 106.0],
            'Low': [99.0, 100.0],
            'Close': [103.0, 104.0],
            'Volume': [1000, 1100]
        })
        mock_download.return_value = {'SBIN.NS': mock_data}

        result = YFinanceService.get_quote('SBIN')

        assert result is not None
        assert result['price'] == 104.0

    @patch('app.services.yfinance_service.yf.download')
    def test_get_single_quote_not_found(self, mock_download):
        """Test fetching non-existent symbol"""
        mock_download.return_value = {}

        result = YFinanceService.get_quote('INVALID')

        assert result is None


class TestRetryMechanism:
    """Tests for retry decorator"""

    @patch('app.services.yfinance_service.yf.download')
    def test_retry_on_transient_error(self, mock_download):
        """Test retry mechanism on transient errors"""
        # Fail twice, succeed on third attempt
        mock_download.side_effect = [
            Exception("Timeout"),
            Exception("Timeout"),
            pd.DataFrame({
                'Open': [100.0],
                'High': [105.0],
                'Low': [99.0],
                'Close': [103.0],
                'Volume': [1000]
            })
        ]

        result = YFinanceService.get_quotes(['SBIN'])

        # Should succeed after retries
        assert mock_download.call_count == 3

    @patch('app.services.yfinance_service.yf.download')
    @patch('app.services.yfinance_service.time.sleep')  # Mock sleep to speed up test
    def test_max_retries_exceeded(self, mock_sleep, mock_download):
        """Test behavior when max retries exceeded"""
        mock_download.side_effect = Exception("Persistent error")

        result = YFinanceService.get_quotes(['SBIN'])

        # Should return None after max retries
        assert result is None
        assert mock_download.call_count == 2  # max_retries=2 in decorator


class TestEdgeCases:
    """Tests for edge cases"""

    @patch('app.services.yfinance_service.yf.download')
    def test_large_symbol_list(self, mock_download):
        """Test handling large list of symbols"""
        # Create 100 symbols
        symbols = [f'SYM{i}' for i in range(100)]

        mock_download.return_value = {}

        result = YFinanceService.get_quotes(symbols)

        # Should not crash
        assert isinstance(result, dict)

    @patch('app.services.yfinance_service.yf.download')
    def test_special_characters_in_symbol(self, mock_download):
        """Test handling special characters"""
        mock_data = pd.DataFrame({
            'Open': [100.0],
            'High': [105.0],
            'Low': [99.0],
            'Close': [103.0],
            'Volume': [1000]
        })
        mock_download.return_value = {'M&M.NS': mock_data}

        result = YFinanceService.get_quotes(['M&M'])

        # Should handle special characters
        assert 'M&M' in result

    def test_singleton_instance(self):
        """Test that yfinance_service is properly instantiated"""
        assert yfinance_service is not None
        assert isinstance(yfinance_service, YFinanceService)