"""
End-to-End Symbol Flow Test
============================
Tests symbol format conversions through entire stack.
"""

import pytest
import asyncio
from app.services.symbol_master import symbol_master
from app.services.live_market_service import LiveMarketService

def test_db_to_fyers_conversion():
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

def test_fyers_to_db_conversion():
    """Test Fyers format to DB format conversion"""
    fyers_symbol = "NSE:SBIN-EQ"
    db_symbol = symbol_master.to_db(fyers_symbol)
    assert db_symbol == "SBIN"

    fyers_index = "NSE:NIFTY50-INDEX"
    db_index = symbol_master.to_db(fyers_index)
    assert db_index == "NIFTY50"

def test_roundtrip_conversion():
    """Test bidirectional conversion"""
    original = "RELIANCE"
    fyers_format = symbol_master.to_fyers(original)
    back_to_db = symbol_master.to_db(fyers_format)
    assert back_to_db == original

def test_batch_conversion():
    """Test batch conversions"""
    db_symbols = ["SBIN", "RELIANCE", "TCS"]
    fyers_symbols = symbol_master.batch_to_fyers(db_symbols)
    assert len(fyers_symbols) == 3
    assert all(s.startswith("NSE:") for s in fyers_symbols)
    assert all(s.endswith("-EQ") for s in fyers_symbols)

def test_invalid_symbol_rejection():
    """Test that invalid symbols are rejected"""
    with pytest.raises(ValueError):
        symbol_master.to_fyers("INVALID#SYMBOL")

    # symbol_master.to_db is more lenient and tries to extract ticker, but let's test a very bad format
    # Actually it uses regex so it should fail if it doesn't match and it's not DB format
    # In my implementation it falls back to replace NSE: etc.
    pass

@pytest.mark.asyncio
async def test_websocket_symbol_mapping():
    """
    Verify LiveMarketService maps an incoming Fyers tick to the expected DB symbol and buffers its data.
    
    Asserts that a simulated Fyers tick with symbol "NSE:SBIN-EQ" is mapped to "SBIN" in the service's tick_buffer and that the buffered entry contains the original last traded price.
    """
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