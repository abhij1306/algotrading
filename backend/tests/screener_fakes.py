"""In-memory fakes for the screener service's collaborators, shared by its tests."""

from typing import Any

from app.services.screener import ScreenerCriteria, ScreenerRow, SortSpec


def row(symbol: str, **values: object) -> ScreenerRow:
    fields: dict[str, Any] = {
        "name": f"{symbol.title()} Ltd",
        "sector": "Banks",
        "market_cap": 1000.0,
        "close": 100.0,
        "volume": 1000,
        "rsi_14": 50.0,
        "macd": 0.5,
        "adx": 20.0,
        "previous_close": 100.0,
    }
    fields.update(values)
    return ScreenerRow(symbol=symbol, **fields)


# Stored values chosen so that every column orders the four symbols differently.
ROWS = [
    row("ALPHA", market_cap=400.0, close=10.0, volume=300, rsi_14=70.0, macd=0.1, adx=15.0),
    row("BRAVO", market_cap=100.0, close=40.0, volume=100, rsi_14=30.0, macd=0.4, adx=35.0),
    row("CHARLIE", market_cap=300.0, close=20.0, volume=400, rsi_14=60.0, macd=0.3, adx=25.0),
    row("DELTA", market_cap=200.0, close=30.0, volume=200, rsi_14=40.0, macd=0.2, adx=45.0),
]

_STORED_KEYS = {
    "symbol": lambda r: r.symbol,
    "marketcap": lambda r: r.market_cap,
    "rsi": lambda r: r.rsi_14,
    "macd": lambda r: r.macd,
    "adx": lambda r: r.adx,
}


class FakeRepository:
    def __init__(self, rows: list[ScreenerRow]) -> None:
        self.rows = rows
        self.fetches: list[tuple[SortSpec | None, int | None, int | None]] = []

    def _match(self, criteria: ScreenerCriteria) -> list[ScreenerRow]:
        matched = [r for r in self.rows if criteria.symbols is None or r.symbol in criteria.symbols]
        if criteria.text:
            matched = [
                r
                for r in matched
                if criteria.text in r.symbol.lower() or criteria.text in (r.name or "").lower()
            ]
        return matched

    def count(self, criteria: ScreenerCriteria) -> int:
        return len(self._match(criteria))

    def fetch(
        self,
        criteria: ScreenerCriteria,
        order: SortSpec | None = None,
        offset: int | None = None,
        limit: int | None = None,
    ) -> list[ScreenerRow]:
        self.fetches.append((order, offset, limit))
        matched = self._match(criteria)
        if order is not None:
            matched.sort(key=lambda r: r.symbol)
            matched.sort(key=_STORED_KEYS[order.field], reverse=order.descending)
        start = offset or 0
        return matched[start : start + limit if limit is not None else None]


class FakeUniverses:
    def __init__(self, members: dict[str, list[str]], error: Exception | None = None) -> None:
        self.members = members
        self.error = error

    def list_available_indices(self) -> list[dict]:
        if self.error is not None:
            raise self.error
        return [
            {"index_code": code, "name": f"Nifty {code[5:]}", "count": len(symbols)}
            for code, symbols in self.members.items()
        ]

    def get_symbols(self, index_code: str) -> list[str]:
        return self.members.get(index_code, [])


class FakeQuotes:
    def __init__(self, quotes: dict[str, dict[str, Any]] | None = None) -> None:
        self.quotes = quotes or {}
        self.requests: list[list[str]] = []
        self.error: Exception | None = None

    def get_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        self.requests.append(list(symbols))
        if self.error is not None:
            raise self.error
        return {s: q for s, q in self.quotes.items() if s in symbols}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now
