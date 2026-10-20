"""Characterization tests for market dashboard overview-v2 and top gainers (fakes, no network)."""

import asyncio
from datetime import date

import pytest
from sqlalchemy.orm import Session

from app.models import Company, HistoricalPrice
from app.models.universe import IndexConstituentHistory, IndexUniverseDefinition
from app.routers import market_dashboard


class FakeLive:
    """Ticks in `ticks` are fresh unless listed in `stale` (cached but too old)."""

    def __init__(
        self, is_open: bool, ticks: dict[str, dict], stale: frozenset[str] = frozenset()
    ) -> None:
        self.is_open = is_open
        self.ticks = ticks
        self.stale = stale
        self.requested: list[list[str]] = []

    def is_market_open(self) -> bool:
        return self.is_open

    async def get_status(self) -> dict:
        return {"connected": self.is_open}

    def get_fresh_tick(self, symbol: str) -> dict | None:
        return None if symbol in self.stale else self.ticks.get(symbol)

    def get_fresh_ticks(self, symbols: list[str]) -> dict[str, dict]:
        self.requested.append(list(symbols))
        fresh = {s: self.get_fresh_tick(s) for s in symbols}
        return {s: tick for s, tick in fresh.items() if tick is not None}


class FakeQuotes:
    def __init__(self, quotes: dict[str, dict | None], only_known: bool = False) -> None:
        self.quotes = quotes
        self.only_known = only_known  # reply only for symbols it has (like a failed batch)
        self.requested: list[list[str]] = []

    def get_quotes(self, symbols: list[str]) -> dict[str, dict | None]:
        self.requested.append(list(symbols))
        if self.only_known:
            return {s: self.quotes[s] for s in symbols if s in self.quotes}
        return {s: self.quotes.get(s) for s in symbols}


def _use(monkeypatch: pytest.MonkeyPatch, live: FakeLive, quotes: FakeQuotes) -> None:
    monkeypatch.setattr(market_dashboard, "live_market", live)
    monkeypatch.setattr(market_dashboard, "yfinance_service", quotes)
    monkeypatch.setattr(market_dashboard, "_top_gainers_cache", {})


@pytest.fixture
def banknifty_close(db_session: Session) -> None:
    if db_session.query(Company).filter(Company.symbol == "BANKNIFTY").first():
        return
    company = Company(symbol="BANKNIFTY", name="Bank Nifty")
    db_session.add(company)
    db_session.flush()
    db_session.add(
        HistoricalPrice(
            company_id=company.id,
            date=date(2026, 2, 3),
            open=1,
            high=1,
            low=1,
            close=48000,
            volume=1,
        )
    )
    db_session.commit()


BANKNIFTY_DB_QUOTE = {
    "price": 48000.0,
    "change_pct": 0,
    "source": "database",
    "date": "2026-02-03",
}


@pytest.mark.usefixtures("banknifty_close")
def test_overview_v2_live_with_database_fallback(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    quotes = FakeQuotes({})
    _use(monkeypatch, FakeLive(True, {"NIFTY50": {"price": 25000.5, "change_pct": 0.4}}), quotes)

    data = asyncio.run(market_dashboard.get_market_overview_v2(db=db_session))

    assert data == {
        "market_status": {"connected": True},
        "is_live": True,
        "indices": {
            "NIFTY50": {"price": 25000.5, "change_pct": 0.4, "source": "live"},
            "BANKNIFTY": BANKNIFTY_DB_QUOTE,
        },
    }
    assert quotes.requested == [["BANKNIFTY"]]


@pytest.mark.usefixtures("banknifty_close")
def test_overview_v2_empty_yfinance_reply_still_uses_database(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #34 review: get_quotes returning {} (not {index: None}) must not skip the DB."""
    _use(monkeypatch, FakeLive(False, {}), FakeQuotes({}, only_known=True))

    data = asyncio.run(market_dashboard.get_market_overview_v2(db=db_session))

    assert data["indices"]["BANKNIFTY"] == BANKNIFTY_DB_QUOTE


@pytest.mark.usefixtures("banknifty_close")
def test_overview_v2_ignores_stale_live_ticks(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = {"NIFTY50": {"ltp": 1.0, "chp": 0.5}, "BANKNIFTY": {"ltp": 2.0, "chp": 9.0}}
    quotes = FakeQuotes({"NIFTY50": {"price": 3.0, "change_pct": 1.0, "source": "yfinance"}})
    _use(monkeypatch, FakeLive(True, ticks, stale=frozenset(ticks)), quotes)

    data = asyncio.run(market_dashboard.get_market_overview_v2(db=db_session))

    assert data["indices"] == {
        "NIFTY50": {"price": 3.0, "change_pct": 1.0, "source": "yfinance"},
        "BANKNIFTY": BANKNIFTY_DB_QUOTE,
    }


def test_overview_v2_closed_uses_yfinance(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    quotes = FakeQuotes({"NIFTY50": {"price": 1.0, "change_pct": 2.0, "source": "yfinance"}})
    live = FakeLive(False, {"NIFTY50": {"ltp": 9.0, "chp": 9.0}})
    _use(monkeypatch, live, quotes)

    data = asyncio.run(market_dashboard.get_market_overview_v2(db=db_session))

    assert data["is_live"] is False
    assert data["indices"]["NIFTY50"] == {"price": 1.0, "change_pct": 2.0, "source": "yfinance"}
    assert (
        "BANKNIFTY" not in data["indices"] or data["indices"]["BANKNIFTY"]["source"] == "database"
    )
    assert quotes.requested == [["NIFTY50", "BANKNIFTY"]]


def test_overview_v2_live_ticks_for_all_indices(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    quotes = FakeQuotes({})
    ticks = {"NIFTY50": {"ltp": 1.0, "chp": 0.5}, "BANKNIFTY": {"ltp": 2.0, "chp": 0}}
    _use(monkeypatch, FakeLive(True, ticks), quotes)

    data = asyncio.run(market_dashboard.get_market_overview_v2(db=db_session))

    assert data["indices"] == {
        "NIFTY50": {"price": 1.0, "change_pct": 0.5, "source": "live"},
        "BANKNIFTY": {"price": 2.0, "change_pct": None, "source": "live"},
    }
    assert quotes.requested == []


@pytest.fixture
def gainers_index(db_session: Session) -> str:
    existing = (
        db_session.query(IndexUniverseDefinition)
        .filter(IndexUniverseDefinition.index_code == "CHARGAIN")
        .first()
    )
    if existing:
        return "CHARGAIN"
    universe = IndexUniverseDefinition(index_code="CHARGAIN", index_name="Char Gainers")
    db_session.add(universe)
    db_session.flush()
    for symbol, start, end in [
        ("AAA", date(2020, 1, 1), None),
        ("BBB", date(2020, 1, 1), date(2099, 1, 1)),
        ("CCC", date(2020, 1, 1), None),
        ("OLD", date(2020, 1, 1), date(2021, 1, 1)),
        ("NEW", date(2099, 1, 1), None),
    ]:
        db_session.add(
            IndexConstituentHistory(
                universe_id=universe.id,
                symbol=symbol,
                effective_from=start,
                effective_to=end,
                import_date=date(2026, 1, 1),
            )
        )
    db_session.commit()
    return "CHARGAIN"


def test_top_gainers_live(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, gainers_index: str
) -> None:
    live = FakeLive(
        True,
        {
            "AAA": {"ltp": 10.0, "chp": 1.5},
            "BBB": {"price": 20.0, "change_pct": 3.0},
            "CCC": {"ltp": 30.0, "chp": -1.0},
        },
    )
    quotes = FakeQuotes({})
    _use(monkeypatch, live, quotes)

    result = market_dashboard.get_top_gainers(limit=1, index=gainers_index, db=db_session)

    assert sorted(live.requested[0]) == ["AAA", "BBB", "CCC"]
    assert result == {
        "data": [{"symbol": "BBB", "price": 20.0, "change_pct": 3.0, "source": "live"}],
        "is_live": True,
        "count": 2,
    }
    assert quotes.requested == []
    # Cached for 15 seconds per (limit, index)
    live.ticks = {}
    assert market_dashboard.get_top_gainers(limit=1, index=gainers_index, db=db_session) is result


def test_top_gainers_yfinance_fallback(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, gainers_index: str
) -> None:
    quotes = FakeQuotes(
        {
            "AAA": {"price": 1.0, "change_pct": 0.5},
            "BBB": {"price": 2.0, "change_pct": 0},
            "CCC": {"price": 3.0, "change_pct": 4.0},
        }
    )
    _use(monkeypatch, FakeLive(False, {"AAA": {"ltp": 9.0, "chp": 5.0}}), quotes)

    result = market_dashboard.get_top_gainers(limit=10, index=gainers_index, db=db_session)

    assert result == {
        "data": [
            {"symbol": "CCC", "price": 3.0, "change_pct": 4.0, "source": "yfinance"},
            {"symbol": "AAA", "price": 1.0, "change_pct": 0.5, "source": "yfinance"},
        ],
        "is_live": False,
        "count": 2,
    }


def test_top_gainers_fall_back_per_constituent_without_a_fresh_tick(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, gainers_index: str
) -> None:
    """PR #34 review: one live tick must not hide the other constituents, and a stale
    tick counts as missing."""
    quotes = FakeQuotes(
        {
            "AAA": {"price": 1.0, "change_pct": 0.5},
            "BBB": {"price": 2.0, "change_pct": 6.0},
            "CCC": {"price": 3.0, "change_pct": 4.0},
        }
    )
    ticks = {"AAA": {"ltp": 9.0, "chp": -2.0}, "BBB": {"ltp": 8.0, "chp": 7.0}}
    _use(monkeypatch, FakeLive(True, ticks, stale=frozenset({"BBB"})), quotes)

    result = market_dashboard.get_top_gainers(limit=10, index=gainers_index, db=db_session)

    assert sorted(quotes.requested[0]) == ["BBB", "CCC"]
    assert result == {
        "data": [
            {"symbol": "BBB", "price": 2.0, "change_pct": 6.0, "source": "yfinance"},
            {"symbol": "CCC", "price": 3.0, "change_pct": 4.0, "source": "yfinance"},
        ],
        "is_live": True,
        "count": 2,
    }


def test_top_gainers_without_constituents(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, FakeLive(False, {}), FakeQuotes({}))

    assert market_dashboard.get_top_gainers(limit=5, index="NOPE", db=db_session) == {
        "data": [],
        "is_live": False,
        "message": "No constituents found for index",
    }
