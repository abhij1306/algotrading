"""
Standard exceptions for data access layer
"""
from fastapi import HTTPException


class DataNotFoundError(HTTPException):
    """Raised when requested data does not exist"""
    def __init__(self, symbol: str, detail: str = None):
        message = detail or f"No data found for symbol: {symbol}"
        super().__init__(status_code=404, detail=message)


class DataSourceUnavailableError(HTTPException):
    """Raised when external data source is down"""
    def __init__(self, source: str, detail: str = None):
        message = detail or f"Data source '{source}' is currently unavailable. Please check connection."
        super().__init__(status_code=503, detail=message)


class InvalidSymbolError(HTTPException):
    """Raised when symbol is malformed or unknown"""
    def __init__(self, symbol: str):
        super().__init__(
            status_code=400,
            detail=f"Invalid or unknown symbol: '{symbol}'"
        )


class MissingTokenError(HTTPException):
    """Raised when Fyers access token is expired/missing"""
    def __init__(self):
        super().__init__(
            status_code=401,
            detail="Fyers access token missing or expired. Please re-authenticate."
        )


class DataIntegrityError(Exception):
    """Raised when data validation fails"""
    pass
