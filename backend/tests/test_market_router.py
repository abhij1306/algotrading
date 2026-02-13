"""
Comprehensive tests for market router endpoints
Tests market status, quotes, search, sectors, and watchlist
"""
import pytest
from unittest.mock import patch, Mock
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


class TestMarketStatus:
    """Tests for /market/status endpoint"""

    @patch('app.routers.market.is_market_open')
    @patch('app.routers.market.get_market_status')
    def test_market_status_open(self, mock_get_status, mock_is_open):
        """Test market status when market is open"""
        mock_get_status.return_value = {
            'is_open': True,
            'message': 'Market is open',
            'time': '10:30 IST'
        }

        response = client.get("/api/market/status")

        assert response.status_code == 200
        data = response.json()
        assert data['is_open'] is True
        assert 'message' in data

    @patch('app.routers.market.is_market_open')
    @patch('app.routers.market.get_market_status')
    def test_market_status_closed(self, mock_get_status, mock_is_open):
        """Test market status when market is closed"""
        mock_get_status.return_value = {
            'is_open': False,
            'message': 'Market is closed',
            'time': '18:00 IST'
        }

        response = client.get("/api/market/status")

        assert response.status_code == 200
        data = response.json()
        assert data['is_open'] is False


class TestLiveQuotes:
    """Tests for /quotes/live endpoint"""

    @patch('app.routers.market.is_market_open')
    @patch('app.routers.market.get_fyers_client')
    def test_get_live_quotes_success(self, mock_get_client, mock_is_open):
        """Test successful live quotes fetch"""
        mock_is_open.return_value = (True, "Market is open")

        mock_client = Mock()
        mock_client.get_parsed_quotes.return_value = {
            'RELIANCE': {
                'ltp': 2500.0,
                'volume': 100000,
                'change_pct': 1.5
            },
            'TCS': {
                'ltp': 3200.0,
                'volume': 50000,
                'change_pct': -0.5
            }
        }
        mock_get_client.return_value = mock_client

        response = client.get("/api/market/quotes/live?symbols=RELIANCE,TCS")

        assert response.status_code == 200
        data = response.json()
        assert 'quotes' in data
        assert 'RELIANCE' in data['quotes']
        assert data['quotes']['RELIANCE']['ltp'] == 2500.0

    @patch('app.routers.market.is_market_open')
    def test_get_live_quotes_market_closed(self, mock_is_open):
        """Test live quotes when market is closed"""
        mock_is_open.return_value = (False, "Market is closed")

        response = client.get("/api/market/quotes/live?symbols=RELIANCE")

        assert response.status_code == 503
        assert 'Live quotes unavailable' in response.json()['detail']

    @patch('app.routers.market.is_market_open')
    @patch('app.routers.market.get_fyers_client')
    def test_get_live_quotes_error_handling(self, mock_get_client, mock_is_open):
        """Test error handling in live quotes"""
        mock_is_open.return_value = (True, "Market is open")

        mock_client = Mock()
        mock_client.get_parsed_quotes.side_effect = Exception("Network error")
        mock_get_client.return_value = mock_client

        response = client.get("/api/market/quotes/live?symbols=RELIANCE")

        assert response.status_code == 200
        data = response.json()
        assert data['quotes'] == {}


class TestSearchSymbols:
    """Tests for /search endpoint"""

    def test_search_symbols_equity(self, client):
        """Test searching for equity symbols"""
        response = client.get("/api/market/search?query=REL")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_search_with_indices(self, client):
        """Test searching including indices"""
        response = client.get("/api/market/search?query=NIFTY&exclude_indices=false")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

        # Should include index results
        has_index = any(item.get('type') == 'INDEX' for item in data)
        assert has_index or len(data) == 0

    def test_search_exclude_indices(self, client):
        """Test searching excluding indices"""
        response = client.get("/api/market/search?query=BANK&exclude_indices=true")

        assert response.status_code == 200
        data = response.json()

        # Should only have equity type
        for item in data:
            assert item.get('type') != 'INDEX'

    def test_search_short_query(self, client):
        """Test search with too short query"""
        response = client.get("/api/market/search?query=R")

        assert response.status_code == 200
        data = response.json()
        assert data == []

    def test_search_no_query(self, client):
        """Test search with empty query"""
        response = client.get("/api/market/search?query=")

        assert response.status_code == 200
        data = response.json()
        assert data == []


class TestGetSectors:
    """Tests for /sectors endpoint"""

    def test_get_sectors(self, client):
        """Test getting list of sectors"""
        response = client.get("/api/market/sectors")

        assert response.status_code == 200
        data = response.json()
        assert 'sectors' in data
        assert isinstance(data['sectors'], list)


class TestWatchlist:
    """Tests for watchlist endpoints"""

    def test_get_empty_watchlist(self, client):
        """Test getting empty watchlist"""
        response = client.get("/api/market/watchlist")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_add_to_watchlist(self, client):
        """Test adding symbol to watchlist"""
        response = client.post(
            "/api/market/watchlist",
            json={"symbol": "RELIANCE", "instrument_type": "EQ"}
        )

        assert response.status_code == 200
        data = response.json()
        assert 'message' in data

    def test_add_to_watchlist_missing_symbol(self, client):
        """Test adding to watchlist without symbol"""
        response = client.post(
            "/api/market/watchlist",
            json={"instrument_type": "EQ"}
        )

        assert response.status_code == 400

    def test_add_duplicate_to_watchlist(self, client):
        """Test adding duplicate symbol to watchlist"""
        # Add first time
        client.post(
            "/api/market/watchlist",
            json={"symbol": "TCS", "instrument_type": "EQ"}
        )

        # Try to add again
        response = client.post(
            "/api/market/watchlist",
            json={"symbol": "TCS", "instrument_type": "EQ"}
        )

        assert response.status_code == 200
        assert "Already in watchlist" in response.json()['message']

    def test_remove_from_watchlist(self, client):
        """Test removing symbol from watchlist"""
        # First add
        client.post(
            "/api/market/watchlist",
            json={"symbol": "INFY", "instrument_type": "EQ"}
        )

        # Then remove
        response = client.delete("/api/market/watchlist/INFY")

        assert response.status_code == 200
        data = response.json()
        assert 'message' in data

    def test_get_watchlist_with_prices(self, client, db_session):
        """Test getting watchlist with price data"""
        from app.models.company import Company
        from app.models.price import HistoricalPrice
        from app.models.market import Watchlist
        from datetime import date

        # Create test company
        company = Company(
            symbol='WATCHTEST',
            name='Watch Test Ltd',
            sector='IT',
            is_active=True
        )
        db_session.add(company)
        db_session.commit()

        # Add price
        price = HistoricalPrice(
            company_id=company.id,
            date=date.today(),
            open=100.0,
            high=105.0,
            low=99.0,
            close=103.0,
            volume=1000000
        )
        db_session.add(price)

        # Add to watchlist
        watchlist_item = Watchlist(
            symbol='WATCHTEST',
            instrument_type='EQ'
        )
        db_session.add(watchlist_item)
        db_session.commit()

        # Get watchlist
        response = client.get("/api/market/watchlist")

        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1

        # Find our item
        watch_item = next((item for item in data if item['symbol'] == 'WATCHTEST'), None)
        assert watch_item is not None
        assert watch_item['ltp'] == 103.0


class TestEdgeCases:
    """Tests for edge cases and error scenarios"""

    def test_invalid_symbols_parameter(self, client):
        """Test with malformed symbols parameter"""
        with patch('app.routers.market.is_market_open', return_value=(True, "Open")):
            response = client.get("/api/market/quotes/live?symbols=")

            assert response.status_code in [200, 503]

    def test_search_special_characters(self, client):
        """Test search with special characters"""
        response = client.get("/api/market/search?query=M%26M")

        assert response.status_code == 200
        # Should handle without crashing

    def test_concurrent_watchlist_operations(self, client):
        """Test concurrent watchlist operations"""
        # Add multiple items
        symbols = ['SYM1', 'SYM2', 'SYM3']

        for sym in symbols:
            response = client.post(
                "/api/market/watchlist",
                json={"symbol": sym, "instrument_type": "EQ"}
            )
            assert response.status_code == 200

        # Get watchlist
        response = client.get("/api/market/watchlist")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 3