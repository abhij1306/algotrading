"""
The screener use case: universe check, filter, sort, pagination and live-quote merge.

`ScreenerService` owns no I/O of its own. Rows come from a `ScreenerRepository`, index
membership from a `UniverseDirectory`, live quotes from a `QuoteSource`, and results are
memoised in a `TTLCache`; production wiring passes the SQL repository, the universe
service, the Fyers quote fetcher and the process-wide cache, and tests pass fakes.

Sorting has two paths. Columns that live quotes can change (`price`, `change`,
`volume`) are sorted in Python after every matching row has been merged with its quote;
stored columns are sorted and paged by the repository, and only the page is hydrated.

Failures the client caused raise `ScreenerError` subclasses whose messages are safe to
return as the response detail.
"""

from __future__ import annotations

import logging
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ...constants.indices import DEFAULT_SCREENER_UNIVERSE
from .cache import TTLCache

logger = logging.getLogger(__name__)

ALL_UNIVERSE = "ALL"
LIVE_SORT_FIELDS = frozenset({"price", "change", "volume"})
STORED_SORT_FIELDS = frozenset({"symbol", "marketcap", "rsi", "macd", "adx"})

Quote = dict[str, Any]
Payload = dict[str, Any]


class ScreenerError(Exception):
    """A request the screener cannot answer. Messages are safe to show to a client."""


class UnknownUniverseError(ScreenerError):
    def __init__(self, universe: str) -> None:
        self.universe = universe
        super().__init__(f"Unknown universe: {universe}")


class InvalidSortFieldError(ScreenerError):
    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"Invalid sort field: {field}")


class UniversesUnavailableError(ScreenerError):
    """The universe list could not be loaded; the cause is logged, not exposed."""

    def __init__(self) -> None:
        super().__init__("Failed to load screener universes")


@dataclass(frozen=True)
class ScreenerRow:
    """One company with its latest stored bar and the close of the bar before it."""

    symbol: str
    name: str | None
    sector: str | None
    market_cap: float | None
    close: float | None
    volume: int | None
    rsi_14: float | None
    macd: float | None
    adx: float | None
    previous_close: float | None


@dataclass(frozen=True)
class ScreenerCriteria:
    """Rows to match: `symbols` (None means every active company) and a text search."""

    symbols: tuple[str, ...] | None
    text: str | None


@dataclass(frozen=True)
class SortSpec:
    """A stored column (one of `STORED_SORT_FIELDS`) and its direction."""

    field: str
    descending: bool


class ScreenerRepository(Protocol):
    def count(self, criteria: ScreenerCriteria) -> int: ...

    def fetch(
        self,
        criteria: ScreenerCriteria,
        order: SortSpec | None = None,
        offset: int | None = None,
        limit: int | None = None,
    ) -> list[ScreenerRow]:
        """Matching rows; ordered by `order` and then by symbol, or unordered if None."""
        ...


class UniverseDirectory(Protocol):
    def list_available_indices(self) -> list[dict]: ...

    def get_symbols(self, index_code: str) -> list[str]: ...


class QuoteSource(Protocol):
    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]: ...


@dataclass(frozen=True)
class ScreenerRequest:
    """Normalised `/results` parameters. `raw_universe` is kept for the error message."""

    raw_universe: str
    universe: str
    query: str | None
    page: int
    limit: int
    full: bool
    sort_by: str
    sort_order: str

    @classmethod
    def from_params(
        cls,
        universe: str,
        query: str | None,
        page: int,
        limit: int,
        full: bool,
        sort_by: str,
        sort_order: str,
    ) -> ScreenerRequest:
        return cls(
            raw_universe=universe,
            universe=universe.upper(),
            query=query.strip().lower() if query else None,
            page=page,
            limit=limit,
            full=bool(full),
            sort_by=(sort_by or "symbol").lower(),
            sort_order=(sort_order or "asc").lower(),
        )

    @property
    def descending(self) -> bool:
        return self.sort_order == "desc"

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.limit

    @property
    def cache_key(self) -> Hashable:
        return (
            self.universe,
            self.query,
            self.page,
            self.limit,
            self.full,
            self.sort_by,
            self.sort_order,
        )


class ScreenerService:
    def __init__(
        self,
        repository: ScreenerRepository,
        universes: UniverseDirectory,
        quotes: QuoteSource,
        cache: TTLCache[Payload],
    ) -> None:
        self._repository = repository
        self._universes = universes
        self._quotes = quotes
        self._cache = cache

    def indices(self) -> Payload:
        """The selectable universes, sorted by name, and the default one."""
        try:
            indices = [
                {
                    "id": item.get("index_code", ""),
                    "name": item.get("name") or item.get("index_code", ""),
                    "count": int(item.get("count") or 0),
                }
                for item in self._universes.list_available_indices()
            ]
            indices.sort(key=lambda item: item["name"])
        except Exception as exc:
            logger.exception("Failed to load screener indices: %s", exc)
            raise UniversesUnavailableError() from exc
        return {
            "indices": indices,
            "default": DEFAULT_SCREENER_UNIVERSE,
            "total_loaded": len(indices),
        }

    def results(self, request: ScreenerRequest) -> Payload:
        """One page of rows (every row when `full`); paged results are cached briefly."""
        if not request.full:
            cached = self._cache.get(request.cache_key)
            if cached is not None:
                return cached

        available = {
            item.get("index_code", "") for item in self._universes.list_available_indices()
        }
        if request.universe != ALL_UNIVERSE and request.universe not in available:
            raise UnknownUniverseError(request.raw_universe)

        payload = self._load(request)
        if not request.full:
            self._cache.set(request.cache_key, payload)
        return payload

    def _load(self, request: ScreenerRequest) -> Payload:
        symbols: tuple[str, ...] | None = None
        if request.universe != ALL_UNIVERSE:
            symbols = tuple(self._universes.get_symbols(request.universe) or ())
            if not symbols:
                return _payload([], 0, request)

        criteria = ScreenerCriteria(symbols=symbols, text=request.query)
        total = self._repository.count(criteria)

        if request.sort_by in LIVE_SORT_FIELDS:
            # TODO(Lane 7.4): cap the live sort at the top K instead of hydrating every row.
            rows = self._repository.fetch(criteria)
            quotes = self._hydrate([row.symbol for row in rows])
            serialized = [serialize_row(row, quotes.get(row.symbol)) for row in rows]
            ordered = sort_serialized_rows(serialized, request.sort_by, request.descending)
            page = (
                ordered
                if request.full
                else ordered[request.offset : request.offset + request.limit]
            )
            return _payload(page, total, request)

        if request.sort_by not in STORED_SORT_FIELDS:
            raise InvalidSortFieldError(request.sort_by)

        rows = self._repository.fetch(
            criteria,
            order=SortSpec(field=request.sort_by, descending=request.descending),
            offset=None if request.full else request.offset,
            limit=None if request.full else request.limit,
        )
        quotes = {} if request.full else self._hydrate([row.symbol for row in rows])
        return _payload(
            [serialize_row(row, quotes.get(row.symbol)) for row in rows], total, request
        )

    def _hydrate(self, symbols: list[str]) -> dict[str, Quote]:
        """Live quotes by symbol; empty when there is nothing to fetch or the fetch fails."""
        if not symbols:
            return {}
        try:
            return self._quotes.get_quotes(symbols) or {}
        except Exception as exc:
            logger.warning("Live quote hydration failed: %s", exc)
            return {}


def _payload(results: list[Payload], total: int, request: ScreenerRequest) -> Payload:
    return {"results": results, "total": total, "page": request.page, "limit": request.limit}


def serialize_row(row: ScreenerRow, quote: Quote | None) -> Payload:
    """The API row: stored values, overridden by a usable live price, change and volume."""
    price = float(row.close or 0)
    volume = int(row.volume or 0)
    prev_close = float(row.previous_close or 0)
    change = ((price - prev_close) / prev_close * 100) if prev_close > 0 else 0.0

    price, volume, change = _apply_quote(price, volume, change, quote)

    return {
        "symbol": row.symbol,
        "name": row.name,
        "sector": row.sector or "Unknown",
        "marketCap": float(row.market_cap or 0),
        "change": round(float(change), 2),
        "price": round(float(price), 2),
        "volume": volume,
        "rsi": round(float(row.rsi_14 or 0), 2),
        "macd": round(float(row.macd or 0), 4),
        "adx": round(float(row.adx or 0), 2),
    }


def _apply_quote(
    price: float, volume: int, change: float, quote: Quote | None
) -> tuple[float, int, float]:
    if not quote:
        return price, volume, change

    quote_price = quote.get("ltp")
    quote_volume = quote.get("volume")
    prev_close = quote.get("prev_close")

    if isinstance(quote_price, (int, float)) and quote_price > 0:
        price = float(quote_price)
        if isinstance(prev_close, (int, float)) and prev_close > 0:
            change = ((price - prev_close) / prev_close) * 100

    if isinstance(quote_volume, (int, float)) and quote_volume > 0:
        volume = int(quote_volume)

    return price, volume, change


def sort_serialized_rows(rows: Sequence[Payload], sort_by: str, descending: bool) -> list[Payload]:
    """Sort API rows by a live column (`LIVE_SORT_FIELDS`), ties by symbol; `descending`
    reverses both."""
    return sorted(rows, key=lambda row: (row[sort_by], row["symbol"]), reverse=descending)
