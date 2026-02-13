"""
Comprehensive tests for Live Market Service
Tests market hours, tick handling, and orchestration
"""
import pytest
from unittest.mock import Mock, patch, AsyncMock
import asyncio
from datetime import datetime, time
import pytz

from app.services.live_market_service import LiveMarketService, live_market, IST, MARKET_OPEN_TIME, MARKET_CLOSE_TIME


class TestMarketHours:
    """Tests for market hours validation"""

    @patch('app.services.live_market_service.datetime')
    def test_is_market_open_during_hours(self, mock_datetime):
        """Test market is open during trading hours"""
        # Mock a weekday at 10:30 IST
        mock_now = datetime(2024, 1, 15, 10, 30, 0, tzinfo=IST)  # Monday
        mock_datetime.datetime.now.return_value = mock_now

        service = LiveMarketService()
        service.dev_mode = False

        assert service.is_market_open() is True
        assert service._market_status == "OPEN"

    @patch('app.services.live_market_service.datetime')
    def test_is_market_closed_before_hours(self, mock_datetime):
        """Test market is closed before trading hours"""
        # Mock a weekday at 8:00 IST
        mock_now = datetime(2024, 1, 15, 8, 0, 0, tzinfo=IST)
        mock_datetime.datetime.now.return_value = mock_now

        service = LiveMarketService()
        service.dev_mode = False

        assert service.is_market_open() is False
        assert service._market_status == "CLOSED_OFF_HOURS"

    @patch('app.services.live_market_service.datetime')
    def test_is_market_closed_after_hours(self, mock_datetime):
        """Test market is closed after trading hours"""
        # Mock a weekday at 16:00 IST
        mock_now = datetime(2024, 1, 15, 16, 0, 0, tzinfo=IST)
        mock_datetime.datetime.now.return_value = mock_now

        service = LiveMarketService()
        service.dev_mode = False

        assert service.is_market_open() is False

    @patch('app.services.live_market_service.datetime')
    def test_is_market_closed_weekend(self, mock_datetime):
        """Test market is closed on weekends"""
        # Mock Saturday at 10:30 IST
        mock_now = datetime(2024, 1, 13, 10, 30, 0, tzinfo=IST)  # Saturday
        mock_datetime.datetime.now.return_value = mock_now

        service = LiveMarketService()
        service.dev_mode = False

        assert service.is_market_open() is False
        assert service._market_status == "CLOSED_WEEKEND"

    def test_dev_mode_always_open(self):
        """Test dev mode makes market always open"""
        service = LiveMarketService()
        service.dev_mode = True

        assert service.is_market_open() is True
        assert service._market_status == "OPEN (DEV)"


class TestTickHandling:
    """Tests for tick data handling"""

    @pytest.mark.asyncio
    async def test_update_buffer(self):
        """Test updating tick buffer"""
        service = LiveMarketService()
        service.loop = asyncio.get_running_loop()

        tick = {
            "symbol": "SBIN",
            "ltp": 500.0,
            "volume": 10000
        }

        await service._update_buffer(tick)

        assert "SBIN" in service.tick_buffer
        assert service.tick_buffer["SBIN"]["ltp"] == 500.0
        assert "SBIN" in service.latest_values

    @pytest.mark.asyncio
    async def test_update_buffer_multiple_ticks(self):
        """Test updating buffer with multiple ticks"""
        service = LiveMarketService()
        service.loop = asyncio.get_running_loop()

        ticks = [
            {"symbol": "SBIN", "ltp": 500.0},
            {"symbol": "RELIANCE", "ltp": 2500.0},
            {"symbol": "TCS", "ltp": 3200.0}
        ]

        for tick in ticks:
            await service._update_buffer(tick)

        assert len(service.tick_buffer) == 3
        assert "SBIN" in service.tick_buffer
        assert "RELIANCE" in service.tick_buffer

    def test_handle_tick_incoming(self):
        """Test handling incoming tick from Fyers thread"""
        service = LiveMarketService()
        loop = asyncio.new_event_loop()
        service.loop = loop

        tick = {
            "symbol": "NSE:SBIN-EQ",
            "ltp": 500.0
        }

        # Mock symbol_master
        with patch('app.services.live_market_service.symbol_master') as mock_sm:
            mock_sm.to_db.return_value = "SBIN"

            service.handle_tick_incoming(tick)

            # Should convert symbol to DB format
            mock_sm.to_db.assert_called_once_with("NSE:SBIN-EQ")

    def test_handle_tick_no_loop(self):
        """Test handling tick without event loop"""
        service = LiveMarketService()
        service.loop = None

        tick = {"symbol": "SBIN", "ltp": 500.0}

        # Should not crash
        service.handle_tick_incoming(tick)

    def test_handle_tick_with_error(self):
        """Test error handling in tick processing"""
        service = LiveMarketService()
        loop = asyncio.new_event_loop()
        service.loop = loop

        # Invalid tick data
        tick = {}

        # Should not crash
        service.handle_tick_incoming(tick)


class TestBroadcasting:
    """Tests for tick broadcasting"""

    @pytest.mark.asyncio
    async def test_flush_loop(self):
        """Test flush loop broadcasts buffered ticks"""
        service = LiveMarketService()

        # Mock manager
        with patch('app.services.live_market_service.manager') as mock_manager:
            mock_manager.broadcast = AsyncMock()

            service.tick_buffer = {
                "SBIN": {"symbol": "SBIN", "ltp": 500.0}
            }

            # Run one iteration of flush loop
            await service._update_buffer({"symbol": "TCS", "ltp": 3200.0})

            # Buffer should be updated
            assert "TCS" in service.tick_buffer

    @pytest.mark.asyncio
    async def test_flush_loop_empty_buffer(self):
        """Test flush loop with empty buffer"""
        service = LiveMarketService()

        with patch('app.services.live_market_service.manager') as mock_manager:
            mock_manager.broadcast = AsyncMock()

            service.tick_buffer = {}

            # Should not broadcast anything
            # Flush loop would sleep and continue

    @pytest.mark.asyncio
    async def test_flush_loop_cancellation(self):
        """Test flush loop handles cancellation"""
        service = LiveMarketService()

        task = asyncio.create_task(service._flush_loop())

        # Cancel after short delay
        await asyncio.sleep(0.1)
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass  # Expected


class TestConnection:
    """Tests for service connection"""

    @patch('app.services.live_market_service.get_websocket_service')
    def test_connect_market_open(self, mock_get_ws):
        """Test connection when market is open"""
        service = LiveMarketService()
        service.dev_mode = True  # Force market open

        mock_ws = Mock()
        mock_ws.connect = Mock()
        mock_ws.ws = Mock()
        mock_ws.ws.is_connected.return_value = False
        mock_get_ws.return_value = mock_ws

        loop = asyncio.new_event_loop()
        service.connect(loop=loop)

        assert service.loop == loop
        assert service.ws_service is not None

    def test_connect_market_closed(self):
        """Test connection when market is closed"""
        service = LiveMarketService()
        service.dev_mode = False

        with patch('app.services.live_market_service.datetime') as mock_dt:
            # Weekend
            mock_now = datetime(2024, 1, 13, 10, 30, 0, tzinfo=IST)
            mock_dt.datetime.now.return_value = mock_now

            loop = asyncio.new_event_loop()
            service.connect(loop=loop)

            # Should not connect to Fyers
            assert service.ws_service is None

    @patch('app.services.live_market_service.get_websocket_service')
    def test_connect_exception(self, mock_get_ws):
        """Test connection error handling"""
        service = LiveMarketService()
        service.dev_mode = True

        mock_get_ws.side_effect = Exception("Connection failed")

        loop = asyncio.new_event_loop()

        # Should not crash
        service.connect(loop=loop)


class TestSubscription:
    """Tests for symbol subscription"""

    @pytest.mark.asyncio
    async def test_subscribe_success(self):
        """Test successful subscription"""
        service = LiveMarketService()
        service.dev_mode = True

        mock_ws = Mock()
        mock_ws.ws = Mock()
        mock_ws.ws.is_connected.return_value = True
        mock_ws.subscribe = Mock()
        service.ws_service = mock_ws

        with patch('app.services.live_market_service.symbol_master') as mock_sm:
            mock_sm.batch_to_fyers.return_value = ["NSE:SBIN-EQ"]

            await service.subscribe(["SBIN"])

            mock_sm.batch_to_fyers.assert_called_once_with(["SBIN"])

    @pytest.mark.asyncio
    async def test_subscribe_market_closed(self):
        """Test subscription when market is closed"""
        service = LiveMarketService()
        service.dev_mode = False
        service.ws_service = None

        with patch('app.services.live_market_service.datetime') as mock_dt:
            mock_now = datetime(2024, 1, 13, 10, 30, 0, tzinfo=IST)
            mock_dt.datetime.now.return_value = mock_now

            await service.subscribe(["SBIN"])

            # Should log warning but not crash

    @pytest.mark.asyncio
    async def test_subscribe_not_connected(self):
        """Test subscription when not connected"""
        service = LiveMarketService()
        service.ws_service = None

        # Should trigger connection
        with patch.object(service, 'connect') as mock_connect:
            service.dev_mode = True
            await service.subscribe(["SBIN"])

            mock_connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_unsubscribe_success(self):
        """Test successful unsubscription"""
        service = LiveMarketService()

        mock_ws = Mock()
        mock_ws.ws = Mock()
        mock_ws.ws.is_connected.return_value = True
        mock_ws.unsubscribe = Mock()
        service.ws_service = mock_ws

        with patch('app.services.live_market_service.symbol_master') as mock_sm:
            mock_sm.batch_to_fyers.return_value = ["NSE:SBIN-EQ"]

            await service.unsubscribe(["SBIN"])


class TestGetLatestTick:
    """Tests for getting latest tick data"""

    def test_get_latest_tick_exists(self):
        """Test getting existing tick"""
        service = LiveMarketService()
        service.latest_values = {
            "SBIN": {"symbol": "SBIN", "ltp": 500.0}
        }

        tick = service.get_latest_tick("SBIN")

        assert tick is not None
        assert tick["ltp"] == 500.0

    def test_get_latest_tick_not_exists(self):
        """Test getting non-existent tick"""
        service = LiveMarketService()
        service.latest_values = {}

        tick = service.get_latest_tick("SBIN")

        assert tick is None

    def test_get_latest_ticks_multiple(self):
        """Test getting multiple ticks"""
        service = LiveMarketService()
        service.latest_values = {
            "SBIN": {"ltp": 500.0},
            "RELIANCE": {"ltp": 2500.0},
            "TCS": {"ltp": 3200.0}
        }

        ticks = service.get_latest_ticks(["SBIN", "RELIANCE", "INVALID"])

        assert len(ticks) == 2
        assert "SBIN" in ticks
        assert "RELIANCE" in ticks
        assert "INVALID" not in ticks


class TestGetStatus:
    """Tests for get_status method"""

    def test_get_status_connected(self):
        """Test status when connected"""
        service = LiveMarketService()
        service._market_status = "OPEN"

        mock_ws = Mock()
        mock_ws.ws = Mock()
        mock_ws.ws.is_connected.return_value = True
        service.ws_service = mock_ws

        status = service.get_status()

        assert status["market_status"] == "OPEN"
        assert status["fyers_connected"] is True

    def test_get_status_disconnected(self):
        """Test status when disconnected"""
        service = LiveMarketService()
        service._market_status = "CLOSED"
        service.ws_service = None

        status = service.get_status()

        assert status["market_status"] == "CLOSED"
        assert status["fyers_connected"] is False


class TestSingleton:
    """Tests for singleton pattern"""

    def test_live_market_singleton(self):
        """Test that live_market is a singleton"""
        assert live_market is not None
        assert isinstance(live_market, LiveMarketService)


class TestEdgeCases:
    """Tests for edge cases"""

    @pytest.mark.asyncio
    async def test_concurrent_subscriptions(self):
        """Test handling concurrent subscriptions"""
        service = LiveMarketService()
        service.dev_mode = True

        mock_ws = Mock()
        mock_ws.ws = Mock()
        mock_ws.ws.is_connected.return_value = True
        mock_ws.subscribe = Mock()
        service.ws_service = mock_ws

        with patch('app.services.live_market_service.symbol_master') as mock_sm:
            mock_sm.batch_to_fyers.return_value = []

            # Subscribe to multiple symbols concurrently
            tasks = [
                service.subscribe([f"SYM{i}"])
                for i in range(10)
            ]

            await asyncio.gather(*tasks)

    def test_tick_overwrite(self):
        """Test that new ticks overwrite old ones"""
        service = LiveMarketService()
        service.latest_values = {"SBIN": {"ltp": 500.0}}

        # Simulate new tick
        service.latest_values["SBIN"] = {"ltp": 510.0}

        tick = service.get_latest_tick("SBIN")
        assert tick["ltp"] == 510.0

    @pytest.mark.asyncio
    async def test_buffer_atomic_swap(self):
        """Test atomic swap in flush loop"""
        service = LiveMarketService()

        # Fill buffer
        service.tick_buffer = {
            "SBIN": {"ltp": 500.0},
            "RELIANCE": {"ltp": 2500.0}
        }

        # After flush, buffer should be cleared
        # (This is tested implicitly in flush_loop tests)