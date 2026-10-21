"""
End-to-End Symbol Flow Test
============================
Tests symbol format conversions through entire stack.
"""

import pytest

from app.services.symbol_master import symbol_master


def test_db_to_fyers_conversion() -> None:
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


def test_fyers_to_db_conversion() -> None:
    """Test Fyers format to DB format conversion"""
    fyers_symbol = "NSE:SBIN-EQ"
    db_symbol = symbol_master.to_db(fyers_symbol)
    assert db_symbol == "SBIN"

    fyers_index = "NSE:NIFTY50"
    db_index = symbol_master.to_db(fyers_index)
    assert db_index == "NIFTY50"


def test_corporate_action_alias_ltim_to_ltm() -> None:
    """Legacy LTIM symbol should map to LTM for provider compatibility."""
    assert symbol_master.to_db("LTIM") == "LTM"
    assert symbol_master.to_db("NSE:LTIM-EQ") == "LTM"
    assert symbol_master.to_fyers("LTIM") == "NSE:LTM-EQ"
    assert symbol_master.to_fyers("NSE:LTIM-EQ") == "NSE:LTM-EQ"


def test_roundtrip_conversion() -> None:
    """Test bidirectional conversion"""
    original = "RELIANCE"
    fyers_format = symbol_master.to_fyers(original)
    back_to_db = symbol_master.to_db(fyers_format)
    assert back_to_db == original


def test_batch_conversion() -> None:
    """Test batch conversions"""
    db_symbols = ["SBIN", "RELIANCE", "TCS"]
    fyers_symbols = symbol_master.batch_to_fyers(db_symbols)
    assert len(fyers_symbols) == 3
    assert all(s.startswith("NSE:") for s in fyers_symbols)
    assert all(s.endswith("-EQ") for s in fyers_symbols)


def test_invalid_symbol_rejection() -> None:
    """Test that invalid symbols are rejected"""
    with pytest.raises(ValueError):
        symbol_master.to_fyers("INVALID#SYMBOL")

    # `to_db` is intentionally lenient, so this test covers only the strict `to_fyers` path.
