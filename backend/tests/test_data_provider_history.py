"""DataProvider.get_history serves intraday timeframes from the IntradayCandle table."""

from datetime import datetime

import pytest
from sqlalchemy.orm import Session

from app.data_layer.exceptions import DataNotFoundError
from app.data_layer.provider import DataProvider
from app.models import Company, IntradayCandle


@pytest.fixture
def intraday_company(db_session: Session) -> Session:
    company = Company(symbol="INTRATEST", name="Intraday Test Ltd")
    db_session.add(company)
    db_session.flush()
    for minute, close in ((15, 101.5), (20, 102.0)):
        db_session.add(
            IntradayCandle(
                company_id=company.id,
                timestamp=datetime(2025, 6, 2, 9, minute),
                timeframe=5,
                open=100.0,
                high=103.0,
                low=99.0,
                close=close,
                volume=1000,
            )
        )
    db_session.flush()
    try:
        yield db_session
    finally:
        db_session.rollback()


def test_five_minute_history_returns_the_stored_candles(intraday_company: Session) -> None:
    df = DataProvider(intraday_company).get_history(
        "INTRATEST",
        timeframe="5MIN",
        start_date=datetime(2025, 6, 2, 9, 0),
        end_date=datetime(2025, 6, 2, 15, 30),
    )

    assert df.to_dict("records") == [
        {
            "timestamp": datetime(2025, 6, 2, 9, 15),
            "open": 100.0,
            "high": 103.0,
            "low": 99.0,
            "close": 101.5,
            "volume": 1000,
        },
        {
            "timestamp": datetime(2025, 6, 2, 9, 20),
            "open": 100.0,
            "high": 103.0,
            "low": 99.0,
            "close": 102.0,
            "volume": 1000,
        },
    ]


def test_intraday_lookup_uses_the_stored_symbol_and_any_timeframe_case(
    intraday_company: Session,
) -> None:
    provider = DataProvider(intraday_company)

    df = provider.get_history(
        "intratest",
        timeframe="5min",
        start_date=datetime(2025, 6, 2, 9, 0),
        end_date=datetime(2025, 6, 2, 15, 30),
    )

    assert df["close"].tolist() == [101.5, 102.0]


def test_intraday_timeframe_without_candles_raises_not_found(intraday_company: Session) -> None:
    provider = DataProvider(intraday_company)
    start, end = datetime(2025, 6, 2, 9, 0), datetime(2025, 6, 2, 15, 30)

    with pytest.raises(DataNotFoundError) as excinfo:
        provider.get_history("INTRATEST", timeframe="15MIN", start_date=start, end_date=end)

    assert excinfo.value.detail == "No 15MIN intraday data available for INTRATEST"


def test_unknown_timeframe_is_rejected(intraday_company: Session) -> None:
    provider = DataProvider(intraday_company)

    with pytest.raises(NotImplementedError) as excinfo:
        provider.get_history("INTRATEST", timeframe="2H")

    assert str(excinfo.value) == (
        "Timeframe '2H' not supported. Use '1D' or one of: 1MIN, 5MIN, 15MIN, 30MIN, 1H."
    )
