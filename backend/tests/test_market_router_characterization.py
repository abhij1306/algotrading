"""Characterization tests for market router watchlist rows."""

from datetime import date, datetime

from sqlalchemy.orm import Session

from app.models import Company, HistoricalPrice, Watchlist
from app.routers import market


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
