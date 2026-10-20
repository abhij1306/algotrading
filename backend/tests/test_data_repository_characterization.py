"""Characterization tests for DataRepository price saving and intraday candle loading."""

import os
from datetime import datetime

import pandas as pd
import pytest
from sqlalchemy.orm import Session

from app import data_repository as repo_module
from app import indicators
from app.data_repository import DataRepository
from app.database import DataUpdateLog, HistoricalPrice, IntradayCandle

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def _daily(start: str, closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c + 1 for c in closes],
            "Low": [c - 1 for c in closes],
            "Close": closes,
            "Volume": [1000] * len(closes),
        },
        index=idx,
    )


def test_save_historical_prices_skips_existing_and_stores_features(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    features = {"ema20": 1.5, "rsi": 55.0, "20d_high": 130.0, "trend_7d": 0.7, "obv": 9.0}
    calls: list[int] = []

    def fake_features(symbol: str, hist: pd.DataFrame) -> dict:
        calls.append(len(hist))
        return features

    monkeypatch.setattr(indicators, "compute_features", fake_features)
    repo = DataRepository(db_session)

    first = repo.save_historical_prices(
        "CHARSAVE", _daily("2026-01-01", [100.0 + i for i in range(25)])
    )
    again = repo.save_historical_prices(
        "CHARSAVE", _daily("2026-01-20", [200.0 + i for i in range(10)])
    )

    assert (first, again) == (25, 4)
    assert len(calls) == 2
    company = repo.get_company("CHARSAVE")
    rows = (
        db_session.query(HistoricalPrice)
        .filter(HistoricalPrice.company_id == company.id)
        .order_by(HistoricalPrice.date)
        .all()
    )
    assert len(rows) == 29
    assert rows[18].close == 118.0  # 2026-01-19 kept from the first save
    assert rows[19].close == 119.0  # 2026-01-20 existed, so the second save skipped it
    latest = rows[-1]
    assert (latest.close, latest.ema_20, latest.rsi_14, latest.high_20d) == (
        209.0,
        1.5,
        55.0,
        130.0,
    )
    assert (latest.is_breakout, latest.trend_7d, latest.obv, latest.macd) == (False, 0.7, 9.0, None)
    logs = db_session.query(DataUpdateLog).filter(DataUpdateLog.company_id == company.id).all()
    assert [(log.records_updated, log.status) for log in logs] == [(25, "success"), (4, "success")]


@pytest.mark.parametrize(
    ("closes", "features", "expected_calls"),
    [([100.0] * 5, {"ema20": 1.0}, 0), ([100.0] * 25, {}, 1), ([100.0] * 25, None, 1)],
)
def test_save_historical_prices_without_features(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    closes: list[float],
    features: dict | None,
    expected_calls: int,
) -> None:
    calls: list[str] = []

    def fake_features(symbol: str, hist: pd.DataFrame) -> dict | None:
        calls.append(symbol)
        return features

    monkeypatch.setattr(indicators, "compute_features", fake_features)
    repo = DataRepository(db_session)
    symbol = f"CHARNF{len(closes)}{expected_calls}{features is None}"

    assert repo.save_historical_prices(symbol, _daily("2026-02-01", closes)) == len(closes)
    assert len(calls) == expected_calls
    company = repo.get_company(symbol)
    latest = (
        db_session.query(HistoricalPrice)
        .filter(HistoricalPrice.company_id == company.id)
        .order_by(HistoricalPrice.date.desc())
        .first()
    )
    assert latest.ema_20 is None


def test_save_historical_prices_survives_feature_errors(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(symbol: str, hist: pd.DataFrame) -> dict:
        raise RuntimeError("bad features")

    monkeypatch.setattr(indicators, "compute_features", boom)
    repo = DataRepository(db_session)

    assert repo.save_historical_prices("CHARBOOM", _daily("2026-03-01", [10.0] * 21)) == 21


def test_save_historical_prices_rolls_back_and_raises_on_bad_rows(db_session: Session) -> None:
    repo = DataRepository(db_session)
    bad = _daily("2026-04-01", [10.0])
    bad["Volume"] = ["not-a-number"]

    with pytest.raises(ValueError, match="not-a-number"):
        repo.save_historical_prices("CHARBAD", bad)


def _add_candles(db_session: Session, symbol: str, timeframe: int) -> None:
    company = DataRepository(db_session).get_or_create_company(symbol)
    for minute, close in [(0, 10.0), (5, 11.0), (10, 12.0)]:
        db_session.add(
            IntradayCandle(
                company_id=company.id,
                timestamp=datetime(2026, 5, 4, 9, 15 + minute),
                timeframe=timeframe,
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=100,
            )
        )
    db_session.commit()


def test_intraday_candles_from_db(db_session: Session) -> None:
    _add_candles(db_session, "CHARINTRA", 5)
    _add_candles(db_session, "CHARINTRA", 15)
    repo = DataRepository(db_session)

    df = repo.get_intraday_candles(
        "CHARINTRA",
        timeframe=5,
        start_date=datetime(2026, 5, 4, 9, 20),
        end_date=datetime(2026, 5, 4, 9, 25),
    )

    assert list(df.columns) == COLUMNS
    assert df["close"].tolist() == [11.0, 12.0]
    assert repo.get_intraday_candles("CHARINTRA", timeframe=5)["close"].tolist() == [
        10.0,
        11.0,
        12.0,
    ]
    assert repo.get_intraday_candles("NOSUCHSYM").empty
    assert repo.get_intraday_candles("CHARINTRA", timeframe=1).empty


@pytest.fixture
def fallback_csv(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve a fake data_system intraday CSV and record which path was read."""
    read: list[str] = []
    real_exists = os.path.exists

    def exists(path: str) -> bool:
        return str(path).endswith("_5min_complete.csv") or real_exists(path)

    def read_csv(path: str) -> pd.DataFrame:
        read.append(str(path))
        return pd.DataFrame(
            {
                "datetime": ["2026-05-04 09:15", "2026-05-04 09:20", "2026-05-05 09:15"],
                "open": [1.0, 2.0, 3.0],
                "high": [1.0, 2.0, 3.0],
                "low": [1.0, 2.0, 3.0],
                "close": [1.0, 2.0, 3.0],
                "volume": [5, 6, 7],
                "extra": ["a", "b", "c"],
            }
        )

    monkeypatch.setattr(os.path, "exists", exists)
    monkeypatch.setattr(repo_module.pd, "read_csv", read_csv)
    return read


@pytest.mark.parametrize(
    ("symbol", "file_name"),
    [("NIFTY 50", "NIFTY50_5min_complete.csv"), ("BANK-NIFTY", "BANKNIFTY_5min_complete.csv")],
)
def test_intraday_candles_csv_fallback(
    db_session: Session, fallback_csv: list[str], symbol: str, file_name: str
) -> None:
    DataRepository(db_session).get_or_create_company(symbol)

    df = DataRepository(db_session).get_intraday_candles(
        symbol, timeframe=5, start_date=datetime(2026, 5, 4, 9, 20)
    )

    expected_dir = os.path.join("data_system", "01_sources", "nse_intraday", file_name)
    assert fallback_csv[0].endswith(expected_dir)
    assert list(df.columns) == COLUMNS
    assert df["close"].tolist() == [2.0, 3.0]
    assert df["timestamp"].iloc[0] == pd.Timestamp("2026-05-04 09:20")


def test_intraday_candles_csv_fallback_only_for_index_5min(
    db_session: Session, fallback_csv: list[str]
) -> None:
    repo = DataRepository(db_session)
    repo.get_or_create_company("NIFTYIT")
    repo.get_or_create_company("CHAROTHER")

    assert repo.get_intraday_candles("NIFTY 50", timeframe=15).empty
    assert repo.get_intraday_candles("CHAROTHER", timeframe=5).empty
    assert repo.get_intraday_candles("NIFTYIT", timeframe=5).empty
    assert fallback_csv == []
