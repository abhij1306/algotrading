"""ScreenerService on in-memory fakes: filter, sort, pagination, live-quote merge, cache.

No database or network: rows come from FakeRepository, membership from FakeUniverses,
live quotes from FakeQuotes, and the cache runs on a fake clock.
"""

from typing import Any

import pytest
from screener_fakes import ROWS, FakeClock, FakeQuotes, FakeRepository, FakeUniverses, row

from app.services.screener import (
    InvalidSortFieldError,
    ScreenerRequest,
    ScreenerRow,
    ScreenerService,
    SortSpec,
    TTLCache,
    UniversesUnavailableError,
    UnknownUniverseError,
)

MEMBERS = {
    "NIFTY50": ["ALPHA", "BRAVO", "CHARLIE", "DELTA"],
    "NIFTYBANK": ["ALPHA", "DELTA"],
    "NIFTYEMPTY": [],
}


class Harness:
    def __init__(
        self,
        rows: list[ScreenerRow] = ROWS,
        quotes: dict[str, dict[str, Any]] | None = None,
        members: dict[str, list[str]] = MEMBERS,
    ) -> None:
        self.repository = FakeRepository(list(rows))
        self.quotes = FakeQuotes(quotes)
        self.universes = FakeUniverses(members)
        self.clock = FakeClock()
        self.cache: TTLCache[dict[str, Any]] = TTLCache(
            ttl_seconds=5.0, maxsize=8, clock=self.clock
        )
        self.service = ScreenerService(self.repository, self.universes, self.quotes, self.cache)

    def results(self, **params: object) -> dict[str, Any]:
        args: dict[str, object] = {
            "universe": "NIFTY50",
            "query": None,
            "page": 1,
            "limit": 50,
            "full": False,
            "sort_by": "symbol",
            "sort_order": "asc",
        }
        args.update(params)
        return self.service.results(ScreenerRequest.from_params(**args))

    def symbols(self, **params: object) -> list[str]:
        return [r["symbol"] for r in self.results(**params)["results"]]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"universe": "NIFTY50"}, ["ALPHA", "BRAVO", "CHARLIE", "DELTA"]),
        ({"universe": "niftybank"}, ["ALPHA", "DELTA"]),
        ({"universe": "ALL"}, ["ALPHA", "BRAVO", "CHARLIE", "DELTA"]),
        ({"universe": "all", "query": "  CHAR "}, ["CHARLIE"]),
        ({"universe": "NIFTY50", "query": "bravo ltd"}, ["BRAVO"]),
        ({"universe": "NIFTYBANK", "query": "bravo"}, []),
    ],
)
def test_filter_by_universe_and_text(params: dict[str, Any], expected: list[str]) -> None:
    assert Harness().symbols(**params) == expected


@pytest.mark.parametrize(
    ("sort_by", "asc"),
    [
        ("symbol", ["ALPHA", "BRAVO", "CHARLIE", "DELTA"]),
        ("marketcap", ["BRAVO", "DELTA", "CHARLIE", "ALPHA"]),
        ("rsi", ["BRAVO", "DELTA", "CHARLIE", "ALPHA"]),
        ("macd", ["ALPHA", "DELTA", "CHARLIE", "BRAVO"]),
        ("adx", ["ALPHA", "CHARLIE", "BRAVO", "DELTA"]),
        ("price", ["ALPHA", "CHARLIE", "DELTA", "BRAVO"]),
        ("volume", ["BRAVO", "DELTA", "ALPHA", "CHARLIE"]),
    ],
)
@pytest.mark.parametrize("sort_order", ["asc", "desc", "DESC"])
def test_sort_direction_per_column(sort_by: str, asc: list[str], sort_order: str) -> None:
    expected = asc if sort_order == "asc" else list(reversed(asc))

    assert Harness().symbols(sort_by=sort_by.upper(), sort_order=sort_order) == expected


def test_change_sorts_on_the_live_quote_not_the_stored_bar() -> None:
    rows = [
        row("ALPHA", close=110.0, previous_close=100.0),  # +10% stored
        row("BRAVO", close=95.0, previous_close=100.0),  # -5% stored
        row("CHARLIE", close=100.0, previous_close=100.0),  # flat, no quote
    ]
    quotes = {
        "ALPHA": {"ltp": 99.0, "prev_close": 100.0, "volume": 10},  # live -1%
        "BRAVO": {"ltp": 103.0, "prev_close": 100.0, "volume": 20},  # live +3%
    }
    harness = Harness(rows=rows, quotes=quotes)

    payload = harness.results(sort_by="change", sort_order="desc")

    assert [(r["symbol"], r["change"]) for r in payload["results"]] == [
        ("BRAVO", 3.0),
        ("CHARLIE", 0.0),
        ("ALPHA", -1.0),
    ]


def test_live_sort_ties_break_on_symbol_in_the_same_direction() -> None:
    rows = [row("BRAVO", volume=5), row("ALPHA", volume=5), row("CHARLIE", volume=1)]
    harness = Harness(rows=rows)

    assert harness.symbols(sort_by="volume", sort_order="asc") == ["CHARLIE", "ALPHA", "BRAVO"]
    assert harness.symbols(sort_by="volume", sort_order="desc") == ["BRAVO", "ALPHA", "CHARLIE"]


def test_an_unknown_sort_order_means_ascending() -> None:
    assert Harness().symbols(sort_by="rsi", sort_order="sideways") == [
        "BRAVO",
        "DELTA",
        "CHARLIE",
        "ALPHA",
    ]


@pytest.mark.parametrize("sort_by", ["symbol", "price"])
@pytest.mark.parametrize(
    ("page", "limit", "expected"),
    [
        (1, 3, ["ALPHA", "BRAVO", "CHARLIE"]),
        (2, 3, ["DELTA"]),
        (3, 3, []),
        (1, 4, ["ALPHA", "BRAVO", "CHARLIE", "DELTA"]),
        (4, 1, ["DELTA"]),
    ],
)
def test_pagination_edges(sort_by: str, page: int, limit: int, expected: list[str]) -> None:
    rows = [row(s, close=float(i)) for i, s in enumerate(["ALPHA", "BRAVO", "CHARLIE", "DELTA"], 1)]

    payload = Harness(rows=rows).results(sort_by=sort_by, page=page, limit=limit)

    assert [r["symbol"] for r in payload["results"]] == expected
    assert (payload["total"], payload["page"], payload["limit"]) == (4, page, limit)


@pytest.mark.parametrize("sort_by", ["symbol", "price"])
def test_full_returns_every_row_whatever_the_page(sort_by: str) -> None:
    payload = Harness().results(sort_by=sort_by, page=3, limit=1, full=True)

    assert len(payload["results"]) == 4
    assert (payload["total"], payload["page"], payload["limit"]) == (4, 3, 1)


def test_a_stored_sort_pages_in_the_repository_and_hydrates_only_that_page() -> None:
    harness = Harness()

    harness.results(sort_by="rsi", sort_order="desc", page=2, limit=2)

    assert harness.repository.fetches == [(SortSpec(field="rsi", descending=True), 2, 2)]
    assert harness.quotes.requests == [["DELTA", "BRAVO"]]


def test_full_skips_live_quotes_on_a_stored_sort_but_not_on_a_live_sort() -> None:
    quotes = {"ALPHA": {"ltp": 12.5, "prev_close": 10.0, "volume": 999}}
    harness = Harness(quotes=quotes)

    stored = harness.results(sort_by="symbol", full=True)["results"][0]
    live = harness.results(sort_by="price", full=True)["results"][0]

    assert (stored["price"], stored["volume"]) == (10.0, 300)
    assert (live["symbol"], live["price"], live["change"], live["volume"]) == (
        "ALPHA",
        12.5,
        25.0,
        999,
    )


def test_a_row_serialises_to_the_api_shape() -> None:
    rows = [
        row(
            "ALPHA",
            name="Alpha Ltd",
            sector=None,
            market_cap=None,
            close=101.234,
            volume=None,
            rsi_14=55.556,
            macd=0.123456,
            adx=None,
            previous_close=100.0,
        )
    ]

    assert Harness(rows=rows).results()["results"] == [
        {
            "symbol": "ALPHA",
            "name": "Alpha Ltd",
            "sector": "Unknown",
            "marketCap": 0.0,
            "change": 1.23,
            "price": 101.23,
            "volume": 0,
            "rsi": 55.56,
            "macd": 0.1235,
            "adx": 0.0,
        }
    ]


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        (None, (100.0, 1000, 0.0)),
        ({"ltp": 0, "prev_close": 50.0, "volume": 0}, (100.0, 1000, 0.0)),
        ({"ltp": 110.0, "prev_close": 0, "volume": 5}, (110.0, 5, 0.0)),
        ({"ltp": 110.0, "prev_close": 88.0, "volume": "n/a"}, (110.0, 1000, 25.0)),
    ],
)
def test_only_usable_quote_fields_override_the_stored_bar(
    quote: dict[str, Any] | None, expected: tuple[float, int, float]
) -> None:
    harness = Harness(rows=[row("ALPHA")], quotes={"ALPHA": quote} if quote else None)

    result = harness.results()["results"][0]

    assert (result["price"], result["volume"], result["change"]) == expected


def test_a_failing_quote_source_falls_back_to_stored_values() -> None:
    harness = Harness(rows=[row("ALPHA", close=110.0, previous_close=100.0)])
    harness.quotes.error = RuntimeError("fyers down")

    result = harness.results(sort_by="price")["results"][0]

    assert (result["price"], result["change"]) == (110.0, 10.0)


@pytest.mark.parametrize(
    "params",
    [
        {"universe": "NIFTYEMPTY"},
        {"universe": "NIFTYEMPTY", "sort_by": "bogus"},
        {"universe": "NIFTY50", "query": "zzz"},
        {"universe": "NIFTY50", "query": "zzz", "sort_by": "price"},
    ],
)
def test_no_matching_rows_is_an_empty_page(params: dict[str, Any]) -> None:
    payload = Harness().results(page=2, limit=10, **params)

    assert payload == {"results": [], "total": 0, "page": 2, "limit": 10}


def test_an_unknown_universe_is_rejected_with_the_name_as_sent() -> None:
    with pytest.raises(UnknownUniverseError, match="^Unknown universe: Nifty999$"):
        Harness().results(universe="Nifty999")


def test_an_unknown_sort_field_is_rejected() -> None:
    with pytest.raises(InvalidSortFieldError, match="^Invalid sort field: bogus$"):
        Harness().results(sort_by="BOGUS")


def test_a_repeat_request_is_served_from_the_cache_until_the_ttl_passes() -> None:
    harness = Harness()
    first = harness.results(sort_by="rsi")
    harness.repository.rows = [row("ZULU")]

    harness.clock.now = 4.9
    assert harness.results(sort_by="RSI", sort_order="ASC") == first
    assert len(harness.repository.fetches) == 1

    harness.clock.now = 5.0
    assert harness.symbols(sort_by="rsi") == []  # ZULU is not in NIFTY50
    assert len(harness.repository.fetches) == 2


@pytest.mark.parametrize(
    "changed",
    [
        {"universe": "NIFTYBANK"},
        {"query": "alpha"},
        {"page": 2},
        {"limit": 10},
        {"sort_by": "adx"},
        {"sort_order": "desc"},
    ],
)
def test_each_parameter_is_part_of_the_cache_key(changed: dict[str, Any]) -> None:
    harness = Harness()
    harness.results()

    harness.results(**changed)

    assert len(harness.repository.fetches) == 2


def test_full_requests_are_never_cached() -> None:
    harness = Harness()

    harness.results(full=True)
    harness.results(full=True)
    harness.results()

    assert len(harness.repository.fetches) == 3


def test_indices_are_sorted_by_name_with_the_default() -> None:
    universes = {"NIFTYBANK": ["A", "B"], "NIFTY50": ["A"], "NIFTYAUTO": []}
    service = ScreenerService(
        FakeRepository([]), FakeUniverses(universes), FakeQuotes(), TTLCache(5.0, 8)
    )

    assert service.indices() == {
        "indices": [
            {"id": "NIFTY50", "name": "Nifty 50", "count": 1},
            {"id": "NIFTYAUTO", "name": "Nifty AUTO", "count": 0},
            {"id": "NIFTYBANK", "name": "Nifty BANK", "count": 2},
        ],
        "default": "NIFTY50",
        "total_loaded": 3,
    }


def test_an_index_without_a_name_is_listed_by_its_code() -> None:
    class Unnamed(FakeUniverses):
        def list_available_indices(self) -> list[dict]:
            return [{"index_code": "NIFTYIT", "name": None, "count": None}]

    service = ScreenerService(FakeRepository([]), Unnamed({}), FakeQuotes(), TTLCache(5.0, 8))

    assert service.indices()["indices"] == [{"id": "NIFTYIT", "name": "NIFTYIT", "count": 0}]


def test_a_failing_universe_list_raises_a_safe_error() -> None:
    universes = FakeUniverses({}, error=RuntimeError("password=hunter2"))
    service = ScreenerService(FakeRepository([]), universes, FakeQuotes(), TTLCache(5.0, 8))

    with pytest.raises(UniversesUnavailableError) as raised:
        service.indices()

    assert str(raised.value) == "Failed to load screener universes"
