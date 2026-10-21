"""Symbol format conversion between HAT's database symbols and Fyers symbols (K2; the
cut-down successor of the legacy `SymbolMaster`, HAT-DATA-024).

Formats: the database format is the bare NSE ticker (`SBIN`, `M&M`); the Fyers format is
`EXCHANGE:TICKER-SERIES` (`NSE:SBIN-EQ`). The conversion is pure: it holds no state, reads no
table and knows nothing about which stocks exist. Which instruments HAT trades comes from
`hat_instrument` and `hat_broker_symbol` (`instruments.fyers_instruments`), never from here.
"""

from __future__ import annotations

import re

__all__ = ["INDEX_SYMBOLS", "SYMBOL_ALIASES", "SymbolMaster", "symbol_master"]

# Fyers symbols: `NSE:SBIN-EQ`, `NSE:NIFTYBANK-INDEX`. The series group is optional.
FYERS_PATTERN = re.compile(
    r"^([A-Z]+):([A-Z0-9&\.\-]+?)(-(EQ|BE|BZ|BL|GS|DR|V|DL|IL|INDEX))?$", re.IGNORECASE
)
DB_PATTERN = re.compile(r"^[A-Z0-9&\.\-]{1,20}$")
# Series suffixes Fyers puts on symbols outside `FYERS_PATTERN` (BE and BZ trade as `-BE`, `-BZ`).
_SERIES_SUFFIXES = ("-EQ", "-INDEX", "-BE", "-BZ", "-BL", "-GS", "-DR", "-V", "-DL", "-IL")

# Index tickers (database format) and the ticker Fyers uses for them where it differs.
INDEX_SYMBOLS: dict[str, str] = {
    "NIFTY50": "NIFTY50",
    "SENSEX": "SENSEX",
    "BANKNIFTY": "NIFTYBANK",  # Fyers calls it NIFTYBANK
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "NIFTYNEXT50": "NIFTYNEXT50",
    "NIFTY100": "NIFTY100",
    "NIFTY200": "NIFTY200",
    "NIFTY500": "NIFTY500",
    "NIFTYIT": "NIFTYIT",
    "NIFTYAUTO": "NIFTYAUTO",
    "NIFTYREALTY": "NIFTYREALTY",
    "INDIAVIX": "INDIAVIX",
    "NIFTYBANK": "NIFTYBANK",
    "NIFTYPHARMA": "NIFTYPHARMA",
    "NIFTYMETAL": "NIFTYMETAL",
}

# Input aliases normalised to the database ticker, including corporate-action renames.
SYMBOL_ALIASES: dict[str, str] = {
    "NIFTY": "NIFTY50",
    "NIFTY 50": "NIFTY50",
    "NIFTY BANK": "BANKNIFTY",
    "BANK NIFTY": "BANKNIFTY",
    "NIFTYBANK": "BANKNIFTY",
    "NIFTY IT": "NIFTYIT",
    "INDIA VIX": "INDIAVIX",
    "S&P BSE SENSEX": "SENSEX",
    "LTIM": "LTM",
}


class SymbolMaster:
    """Pure symbol conversion. Instances are interchangeable; `symbol_master` is the shared one."""

    FYERS_PATTERN = FYERS_PATTERN
    DB_PATTERN = DB_PATTERN
    INDEX_SYMBOLS = INDEX_SYMBOLS
    SYMBOL_ALIASES = SYMBOL_ALIASES
    FYERS_INDEX_MAPPING = INDEX_SYMBOLS

    def is_db_format(self, symbol: str) -> bool:
        return bool(symbol) and bool(DB_PATTERN.match(symbol.strip().upper()))

    def is_fyers_format(self, symbol: str) -> bool:
        return bool(symbol) and bool(FYERS_PATTERN.match(symbol.strip().upper()))

    def to_fyers(self, symbol: str) -> str:
        """The Fyers symbol of `symbol` (any format): `NSE:SBIN-EQ`, or `NSE:NIFTYBANK-INDEX`
        for an index. Raises ValueError when `symbol` is in neither format."""
        known = (symbol or "").strip().upper() in SYMBOL_ALIASES
        if not known and not self.is_db_format(symbol) and not self.is_fyers_format(symbol):
            raise ValueError(f"Invalid symbol format: {symbol}")
        ticker = self.to_db(symbol)
        if ticker in INDEX_SYMBOLS:
            exchange = "BSE" if ticker == "SENSEX" else "NSE"
            return f"{exchange}:{INDEX_SYMBOLS[ticker]}-INDEX"
        return f"NSE:{ticker}-EQ"

    def to_db(self, symbol: str) -> str:
        """The database ticker of `symbol` (any format): `NSE:SBIN-EQ` and `SBIN` give `SBIN`;
        aliases and renames (`LTIM`) give the current ticker. Blank gives blank; text in neither
        format comes back upper-cased and unchanged."""
        if not symbol:
            return ""
        symbol = symbol.strip().upper()
        alias = SYMBOL_ALIASES.get(symbol)
        if alias:
            return alias
        match = FYERS_PATTERN.match(symbol)
        if match:
            ticker = match.group(2)
            return SYMBOL_ALIASES.get(ticker, ticker)
        if ":" in symbol:
            ticker = symbol.split(":")[1]
            for suffix in _SERIES_SUFFIXES:
                if ticker.endswith(suffix):
                    ticker = ticker[: -len(suffix)]
                    break
            return SYMBOL_ALIASES.get(ticker, ticker)
        if DB_PATTERN.match(symbol):
            return SYMBOL_ALIASES.get(symbol, symbol)
        return symbol


symbol_master = SymbolMaster()
