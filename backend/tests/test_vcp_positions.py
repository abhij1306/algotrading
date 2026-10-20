"""VCP positions and status through PositionService, with a fake QuoteSource and SQLite."""

from collections.abc import Generator, Iterable
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import data_fetcher
from app.base import Base
from app.models import StrategyPosition
from app.services.vcp.positions import PositionService
from app.services.vcp.quotes import FyersQuoteSource
from app.services.vcp_service import VCPService
from app.utils.clock import FixedClock

# 20:00 UTC on 20 Oct is already 21 Oct in IST.
NOW = datetime(2026, 10, 20, 20, 0, tzinfo=UTC)


class FakeQuotes:
    """Returns the configured price per symbol; symbols not configured are unavailable."""

    def __init__(self, prices: dict[str, str | None]) -> None:
        self.prices = prices

    def get_last_prices(self, symbols: Iterable[str]) -> dict[str, Decimal | None]:
        out: dict[str, Decimal | None] = {}
        for symbol in symbols:
            price = self.prices.get(symbol)
            out[symbol] = Decimal(price) if price is not None else None
        return out


@pytest.fixture
def factory() -> Generator[sessionmaker, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    try:
        yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def add_position(factory: sessionmaker, symbol: str, **overrides: object) -> int:
    fields: dict[str, object] = {
        "symbol": symbol,
        "entry_date": date(2026, 10, 1),
        "entry_price": 100.0,
        "stop_price": 95.0,
        "shares": 10,
        "shares_remaining": 10,
        "two_r_price": 110.0,
        "regime_at_entry": "BULL",
        "risk_pct_at_entry": 0.01,
        "status": "OPEN",
    }
    fields.update(overrides)
    with factory() as db:
        row = StrategyPosition(**fields)
        db.add(row)
        db.commit()
        return row.id


def service(factory: sessionmaker, prices: dict[str, str | None]) -> PositionService:
    @contextmanager
    def scope() -> Generator[Session, None, None]:
        db = factory()
        try:
            yield db
            db.commit()
        finally:
            db.close()

    return PositionService(FakeQuotes(prices), session_scope=scope, clock=FixedClock(NOW))


def test_open_position_between_stop_and_target(factory: sessionmaker) -> None:
    position_id = add_position(factory, "SBIN")

    result = service(factory, {"SBIN": "104.5"}).list_positions()

    assert result == {
        "positions": [
            {
                "id": position_id,
                "symbol": "SBIN",
                "entry_date": "2026-10-01",
                "entry_price": 100.0,
                "shares": 10,
                "stop_price": 95.0,
                "ltp": 104.5,
                "unrealized_pnl": 45.0,
                "unrealized_pnl_pct": 4.5,
                "r_multiple": 0.9,
                "two_r_status": "NOT_HIT",
                "price_status": "OPEN",
                "status": "OPEN",
            }
        ]
    }


def test_price_at_or_below_stop_is_stop_hit(factory: sessionmaker) -> None:
    add_position(factory, "SBIN")

    [row] = service(factory, {"SBIN": "94"}).list_positions()["positions"]

    assert row["price_status"] == "STOP_HIT"
    assert row["unrealized_pnl"] == -60.0
    assert row["r_multiple"] == -1.2


def test_price_at_or_above_two_r_is_target_hit(factory: sessionmaker) -> None:
    add_position(factory, "SBIN", two_r_hit=True, status="OPEN_PARTIAL", shares_remaining=5)

    [row] = service(factory, {"SBIN": "110"}).list_positions()["positions"]

    assert row["price_status"] == "TARGET_HIT"
    assert row["two_r_status"] == "HIT"
    assert row["status"] == "OPEN_PARTIAL"
    assert row["unrealized_pnl"] == 50.0
    assert row["r_multiple"] == 2.0


def test_missing_quote_is_price_unavailable_not_zero(factory: sessionmaker) -> None:
    add_position(factory, "SBIN")

    [row] = service(factory, {}).list_positions()["positions"]

    assert row["price_status"] == "PRICE_UNAVAILABLE"
    assert row["ltp"] is None
    assert row["unrealized_pnl"] is None
    assert row["unrealized_pnl_pct"] is None
    assert row["r_multiple"] is None


def test_closed_positions_are_not_listed(factory: sessionmaker) -> None:
    add_position(factory, "SBIN", status="STOPPED_OUT")

    assert service(factory, {"SBIN": "100"}).list_positions() == {"positions": []}


def test_close_at_last_price_records_pnl_on_the_ist_date(factory: sessionmaker) -> None:
    position_id = add_position(factory, "SBIN")

    result = service(factory, {"SBIN": "103.25"}).close_position(position_id)

    assert result == {
        "status": "ok",
        "position_id": position_id,
        "exit_price": 103.25,
        "quote_source": "ltp",
        "ltp_missing": False,
    }
    with factory() as db:
        row = db.get(StrategyPosition, position_id)
        assert (row.status, row.exit_date, row.exit_price) == (
            "MANUALLY_CLOSED",
            date(2026, 10, 21),
            103.25,
        )
        assert (row.pnl_inr, row.pnl_pct, row.shares_remaining) == (32.5, 3.25, 0)


def test_close_without_a_price_falls_back_to_entry_and_says_so(factory: sessionmaker) -> None:
    position_id = add_position(factory, "SBIN")

    result = service(factory, {}).close_position(position_id)

    assert result["exit_price"] == 100.0
    assert result["quote_source"] == "entry_fallback"
    assert result["ltp_missing"] is True
    with factory() as db:
        row = db.get(StrategyPosition, position_id)
        assert row.pnl_inr == 0.0
        assert row.notes == "manual_close_quote_fallback symbol=SBIN ltp=unavailable"


def test_close_unknown_position_raises(factory: sessionmaker) -> None:
    with pytest.raises(ValueError, match="Position not found"):
        service(factory, {}).close_position(999)


def test_update_stop_moves_the_stop_status_reads(factory: sessionmaker) -> None:
    position_id = add_position(factory, "SBIN")
    positions = service(factory, {"SBIN": "98"})

    assert positions.update_stop(position_id, 98.5) == {
        "status": "ok",
        "position_id": position_id,
        "stop_price": 98.5,
    }
    [row] = positions.list_positions()["positions"]
    assert row["price_status"] == "STOP_HIT"


def test_halt_resume_and_status_counts(factory: sessionmaker) -> None:
    add_position(factory, "SBIN")
    add_position(factory, "TCS", status="OPEN_PARTIAL")
    add_position(factory, "INFY", status="MANUALLY_CLOSED")
    positions = service(factory, {})

    positions.halt("drawdown")
    status = positions.get_status("nifty50", regime=lambda: {"regime": "BULL"})

    assert status["universe"] == "NIFTY50"
    assert status["halted"] is True
    assert status["halt_reason"] == "drawdown"
    assert status["halted_at"] == NOW.isoformat()
    assert status["open_positions"] == 2
    assert status["pending_entries"] == 0
    assert status["latest_scan_date"] is None
    assert status["regime"] == {"regime": "BULL"}

    positions.resume()
    assert positions.get_status(None, regime=dict)["halted"] is False


def test_vcp_service_delegates_positions(factory: sessionmaker) -> None:
    add_position(factory, "SBIN")

    [row] = VCPService(positions=service(factory, {"SBIN": "101"})).list_positions()["positions"]

    assert row["ltp"] == 101.0


def test_fyers_quote_source_reports_missing_and_zero_prices_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        data_fetcher,
        "fetch_fyers_quotes",
        lambda symbols: {"SBIN": {"ltp": 812.35}, "TCS": {"ltp": 0}, "INFY": {}},
    )

    prices = FyersQuoteSource().get_last_prices(["SBIN", "TCS", "INFY", "HDFC"])

    assert prices == {"SBIN": Decimal("812.35"), "TCS": None, "INFY": None, "HDFC": None}
