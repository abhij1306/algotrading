"""Characterization tests for market router option search and watchlist rows."""

from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.models import Company, HistoricalPrice, Watchlist
from app.routers import market
from app.services import option_chain_service as chain_module


def _leg(symbol: str, ltp: float) -> SimpleNamespace:
    return SimpleNamespace(symbol=symbol, ltp=ltp)


def _chain(underlying: str, strikes: list[float], expiry: date | None) -> SimpleNamespace:
    rows = []
    for i, strike in enumerate(strikes):
        call = _leg(f"{underlying}{strike:g}CE", 10.0 + i) if i != 1 else None
        put = _leg(f"{underlying}{strike:g}PE", 20.0 + i) if i != 2 else None
        rows.append(SimpleNamespace(strike_price=strike, call=call, put=put))
    return SimpleNamespace(strikes=rows, expiry=expiry)


class FakeChains:
    def __init__(self, chains: dict[str, object]) -> None:
        self.chains = chains
        self.calls: list[tuple[str, int]] = []

    def get_option_chain(self, underlying: str, strike_count: int) -> object:
        self.calls.append((underlying, strike_count))
        chain = self.chains.get(underlying)
        if isinstance(chain, Exception):
            raise chain
        return chain


def _use_chains(monkeypatch: pytest.MonkeyPatch, chains: dict[str, object]) -> FakeChains:
    fake = FakeChains(chains)
    monkeypatch.setattr(chain_module, "option_chain_service", fake)
    return fake


def test_search_options_rows_and_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _use_chains(
        monkeypatch,
        {
            "NIFTY": _chain("NIFTY", [100.0 * i for i in range(1, 9)], date(2026, 1, 29)),
            "NIFTY50": _chain("NIFTY50", [1.0, 2.0], None),
            "BANKNIFTY": _chain("BANKNIFTY", [5.0], None),
        },
    )

    rows = market._search_option_symbols("NIFTY", [])

    assert fake.calls == [("NIFTY", 6), ("NIFTY50", 6), ("BANKNIFTY", 6)]
    assert rows[0] == {
        "symbol": "NIFTY100CE",
        "name": "NIFTY 100.0 CE (Call)",
        "sector": "OPTIONS",
        "type": "CE",
        "underlying": "NIFTY",
        "strike": 100.0,
        "expiry": "2026-01-29",
        "ltp": 10.0,
        "instrument_type": "CE",
    }
    assert [r["symbol"] for r in rows] == [
        "NIFTY100CE",
        "NIFTY100PE",
        "NIFTY200PE",
        "NIFTY300CE",
        "NIFTY400CE",
        "NIFTY400PE",
        "NIFTY500CE",
        "NIFTY500PE",
        "NIFTY600CE",
        "NIFTY600PE",
        "NIFTY700CE",
        "NIFTY700PE",
        # The limit is per underlying (PR #34 review): NIFTY's 12 rows leave NIFTY50 whole.
        "NIFTY501CE",
        "NIFTY501PE",
        "NIFTY502PE",
        "BANKNIFTY5CE",
        "BANKNIFTY5PE",
    ]
    assert rows[12]["expiry"] is None
    assert rows[12]["name"] == "NIFTY50 1.0 CE (Call)"


def test_search_options_from_existing_results(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _use_chains(
        monkeypatch,
        {"SBIN": RuntimeError("chain down"), "TCS": _chain("TCS", [3000.0], None)},
    )
    existing = [
        {"symbol": "SBIN", "type": "EQUITY"},
        {"symbol": "TCS", "type": "INDEX"},
        {"symbol": "INFY", "type": "CE"},
        {"symbol": "UNKNOWN", "type": "EQUITY"},
    ]

    rows = market._search_option_symbols("ZZ", existing)

    assert fake.calls == [("SBIN", 6), ("TCS", 6)]
    assert [r["symbol"] for r in rows] == ["TCS3000CE", "TCS3000PE"]


def test_search_options_matches_by_name_and_skips_empty_chains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _use_chains(monkeypatch, {"LT": SimpleNamespace(strikes=[], expiry=None)})

    assert market._search_option_symbols("TOUBRO", []) == []
    assert fake.calls == [("LT", 6)]
    assert market._search_option_symbols("QQQ", []) == []
    assert fake.calls == [("LT", 6)]


def test_watchlist_rows(db_session: Session) -> None:
    db_session.query(Watchlist).delete()
    with_change = Company(symbol="WLCHG", name="Change Co")
    no_open = Company(symbol="WLZERO", name=None)
    no_prices = Company(symbol="WLNOPX", name="No Prices")
    db_session.add_all([with_change, no_open, no_prices])
    db_session.flush()
    db_session.add_all(
        [
            HistoricalPrice(
                company_id=with_change.id,
                date=date(2026, 1, 1),
                open=90,
                high=1,
                low=1,
                close=1,
                volume=1,
            ),
            HistoricalPrice(
                company_id=with_change.id,
                date=date(2026, 1, 2),
                open=100,
                high=1,
                low=1,
                close=103.456,
                volume=1,
            ),
            HistoricalPrice(
                company_id=no_open.id,
                date=date(2026, 1, 2),
                open=0,
                high=1,
                low=1,
                close=50,
                volume=1,
            ),
        ]
    )
    db_session.commit()
    for i, symbol in enumerate(["WLCHG", "WLZERO", "WLNOPX", "WLMISSING"]):
        db_session.add(
            Watchlist(symbol=symbol, instrument_type="EQ", added_at=datetime(2026, 1, i + 1))
        )
    db_session.commit()

    rows = market.get_watchlist(db=db_session)

    def row(
        symbol: str, name: str, ltp: float | None, change: float | None, pct: float | None
    ) -> dict[str, object]:
        return {
            "symbol": symbol,
            "name": name,
            "price": ltp,
            "ltp": ltp,
            "change": change,
            "changePercent": pct,
            "instrument_type": "EQ",
        }

    assert rows == [
        row("WLMISSING", "WLMISSING", None, None, None),
        row("WLNOPX", "No Prices", None, None, None),
        row("WLZERO", "WLZERO", 50.0, None, None),
        row("WLCHG", "Change Co", 103.456, 3.46, 3.46),
    ]
    db_session.query(Watchlist).delete()
    db_session.commit()
    assert market.get_watchlist(db=db_session) == []
