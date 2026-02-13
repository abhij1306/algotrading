"""
Symbol Master Utility
Converts symbols between DB format and Fyers format.
"""
import re
from typing import List

class SymbolMaster:
    """
    Utility for symbol format conversions and validation.

    DB_FORMAT: 'SBIN'
    FYERS_FORMAT: 'NSE:SBIN-EQ'
    """

    def to_fyers_list(self, symbols: List[str]) -> List[str]:
        """
        Convert list of DB_FORMAT symbols to FYERS_FORMAT

        Args:
            symbols: List like ['SBIN', 'RELIANCE']

        Returns:
            List like ['NSE:SBIN-EQ', 'NSE:RELIANCE-EQ']
        """
        return [self.to_fyers(symbol) for symbol in symbols]

    def to_fyers(self, symbol: str, validate: bool = True) -> str:
        """
        Convert single symbol to Fyers format

        Args:
            symbol: DB_FORMAT symbol like 'SBIN'
            validate: Whether to validate symbol exists

        Returns:
            FYERS_FORMAT like 'NSE:SBIN-EQ'
        """
        # If already in Fyers format, return as-is (idempotent)
        if ':' in symbol and '-' in symbol:
            return symbol

        # Some special cases
        if symbol == "NIFTY50": return "NSE:NIFTY50-INDEX"
        if symbol == "BANKNIFTY": return "NSE:BANKNIFTY-INDEX"

        # Validate ticker if requested
        if validate and not self._is_valid_ticker(symbol):
            # Try to handle some common variations (e.g. BAJAJ-AUTO -> BAJAJ_AUTO for DB but BAJAJ-AUTO for NSE?)
            # Actually Fyers usually uses BAJAJ-AUTO-EQ for BAJAJ-AUTO
            pass

        # Convert to Fyers format
        return f"NSE:{symbol.upper()}-EQ"

    def to_db(self, symbol: str, validate: bool = True) -> str:
        """
        Convert Fyers format to DB format

        Args:
            symbol: Any format ('SBIN' or 'NSE:SBIN-EQ')
            validate: Whether to validate

        Returns:
            DB_FORMAT like 'SBIN'
        """
        # If already DB format (no colon, no dash)
        if ':' not in symbol and '-' not in symbol:
            return symbol.upper()

        # Handle index symbols
        if "-INDEX" in symbol:
            return symbol.split(':')[1].split('-')[0]

        # Parse Fyers format: NSE:SBIN-EQ → SBIN
        match = re.match(r'([A-Z]+):([A-Z0-9\-_]+)-([A-Z]+)', symbol)
        if match:
            exchange, ticker, series = match.groups()
            return ticker

        if validate:
            # Fallback for some non-standard formats
            if ':' in symbol:
                return symbol.split(':')[1].split('-')[0]

        return symbol  # Return as-is if can't parse and not validating

    def _is_valid_ticker(self, ticker: str) -> bool:
        """Check if ticker is valid format"""
        return bool(re.match(r'^[A-Z0-9\-_]+$', ticker)) and len(ticker) <= 30

# Singleton instance
symbol_master = SymbolMaster()
