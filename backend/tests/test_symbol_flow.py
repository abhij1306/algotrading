"""
End-to-End Symbol Flow Test
============================
Tests symbol format conversions through entire stack.
"""

import pytest
import asyncio
from app.services.symbol_master import symbol_master, SymbolFormat
from app.services.live_market_service import LiveMarketService


class TestBasicConversions:
    """Tests for basic symbol conversions"""

    def test_db_to_fyers_conversion(self):
        """Test DB format to Fyers format conversion"""
        db_symbol = "SBIN"
        fyers_symbol = symbol_master.to_fyers(db_symbol)
        assert fyers_symbol == "NSE:SBIN-EQ"

        index_symbol = "NIFTY50"
        fyers_index = symbol_master.to_fyers(index_symbol)
        assert fyers_index == "NSE:NIFTY50-INDEX"

        # Test ticker with &
        special_symbol = "M&M"
        fyers_special = symbol_master.to_fyers(special_symbol)
        assert fyers_special == "NSE:M&M-EQ"

    def test_fyers_to_db_conversion(self):
        """Test Fyers format to DB format conversion"""
        fyers_symbol = "NSE:SBIN-EQ"
        db_symbol = symbol_master.to_db(fyers_symbol)
        assert db_symbol == "SBIN"

        fyers_index = "NSE:NIFTY50-INDEX"
        db_index = symbol_master.to_db(fyers_index)
        assert db_index == "NIFTY50"

    def test_roundtrip_conversion(self):
        """Test bidirectional conversion"""
        original = "RELIANCE"
        fyers_format = symbol_master.to_fyers(original)
        back_to_db = symbol_master.to_db(fyers_format)
        assert back_to_db == original


class TestBatchConversions:
    """Tests for batch conversions"""

    def test_batch_conversion(self):
        """Test batch conversions"""
        db_symbols = ["SBIN", "RELIANCE", "TCS"]
        fyers_symbols = symbol_master.batch_to_fyers(db_symbols)
        assert len(fyers_symbols) == 3
        assert all(s.startswith("NSE:") for s in fyers_symbols)
        assert all(s.endswith("-EQ") for s in fyers_symbols)

    def test_batch_to_db(self):
        """Test batch DB conversions"""
        fyers_symbols = ["NSE:SBIN-EQ", "NSE:RELIANCE-EQ"]
        db_symbols = symbol_master.batch_to_db(fyers_symbols)
        assert len(db_symbols) == 2
        assert db_symbols == ["SBIN", "RELIANCE"]


class TestValidation:
    """Tests for symbol validation"""

    def test_invalid_symbol_rejection(self):
        """Test that invalid symbols are rejected"""
        with pytest.raises(ValueError):
            symbol_master.to_fyers("INVALID#SYMBOL")

    def test_validation_methods(self):
        """Test validation methods"""
        assert symbol_master.is_valid("SBIN", SymbolFormat.DB_FORMAT) is True
        assert symbol_master.is_valid("NSE:SBIN-EQ", SymbolFormat.FYERS_FORMAT) is True


class TestEdgeCases:
    """Tests for edge cases"""

    def test_display_format(self):
        """Test display format"""
        assert symbol_master.to_display("NSE:SBIN-EQ") == "SBIN"
        assert symbol_master.to_display("SBIN") == "SBIN"

    def test_lowercase_conversion(self):
        """Test lowercase conversion"""
        result = symbol_master.to_fyers("sbin")
        assert result == "NSE:SBIN-EQ"

    def test_idempotent_fyers(self):
        """Test idempotent Fyers conversion"""
        fyers = "NSE:SBIN-EQ"
        result = symbol_master.to_fyers(fyers)
        assert result == fyers


@pytest.mark.asyncio
async def test_websocket_symbol_mapping():
    """Test that LiveMarketService correctly maps symbols in ticks"""
    # Create service
    service = LiveMarketService()
    service.loop = asyncio.get_running_loop()

    # Mock tick from Fyers
    fyers_tick = {
        "symbol": "NSE:SBIN-EQ",
        "ltp": 500.50,
        "v": 1000
    }

    # Directly call handle_tick_incoming (it's synchronous but calls run_coroutine_threadsafe)
    # We need to wait for the buffer to be updated
    service.handle_tick_incoming(fyers_tick)

    # Give it a tiny bit of time to process in the loop
    await asyncio.sleep(0.1)

    # Check buffer
    assert "SBIN" in service.tick_buffer
    assert service.tick_buffer["SBIN"]["symbol"] == "SBIN"
    assert service.tick_buffer["SBIN"]["ltp"] == 500.50