from collections.abc import Callable, Generator
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.base import Base
from app.database import ensure_backtest_schema
from app.models.backtest import BacktestRun
from app.services import backtest_phase1_service as backtest_module
from app.services.backtest_phase1_service import Phase1BacktestService


@pytest.fixture
def db_session_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[Callable[[], Session], None, None]:
    db_path = tmp_path / "backtest_persistence.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    monkeypatch.setattr(backtest_module, "get_db_session", testing_session_local)
    try:
        yield testing_session_local
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _mock_universe_prices(start: str, end: str) -> pd.DataFrame:
    dates = pd.date_range(start, end, freq="D")
    prices = [1000.0 + (idx * 10.0) for idx in range(len(dates))]
    return pd.DataFrame(
        {
            "date": [d.date() for d in dates],
            "symbol": ["NIFTY50"] * len(dates),
            "price": prices,
        }
    )


def test_create_job_persists_backtest_run(
    db_session_factory: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = Phase1BacktestService()
    monkeypatch.setattr(
        service,
        "_load_universe_index_dataset",
        lambda universe, start, end: _mock_universe_prices("2025-01-01", "2025-01-05"),
    )

    payload = {
        "name": "Persistence check",
        "instrument_type": "equity",
        "start_date": "2025-01-01",
        "end_date": "2025-01-05",
        "initial_capital": 500000.0,
        "selection": {"mode": "universe", "universe": "NIFTY50"},
        "strategies": [
            {
                "strategy_id": "MOMENTUM_2D",
                "weight": 1.0,
                "enabled": True,
                "params": {"lookback_days": 2},
                "strategy_version": "research-2026-03",
                "param_schema_version": 3,
            }
        ],
        "execution": {"slippage_bps": 5},
    }

    result = service.create_job(payload)
    assert result["status"] == "completed"

    session = db_session_factory()
    try:
        run = session.query(BacktestRun).filter(BacktestRun.run_id == result["job_id"]).first()
        assert run is not None
        assert run.status == "completed"
        assert run.request_payload["name"] == "Persistence check"
        assert run.strategy_versions[0]["strategy_version"] == "research-2026-03"
        assert run.strategy_versions[0]["param_schema_version"] == 3
        assert run.result_payload is not None
        assert run.summary_metrics is not None
    finally:
        session.close()


def test_list_runs_and_get_job_read_from_database(
    db_session_factory: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = Phase1BacktestService()
    monkeypatch.setattr(
        service,
        "_load_universe_index_dataset",
        lambda universe, start, end: _mock_universe_prices("2025-01-01", "2025-01-05"),
    )

    payload = {
        "instrument_type": "equity",
        "start_date": "2025-01-01",
        "end_date": "2025-01-05",
        "initial_capital": 1000000.0,
        "selection": {"mode": "universe", "universe": "NIFTY50"},
        "strategies": [
            {"strategy_id": "MOMENTUM_2D", "weight": 1.0, "enabled": True, "params": {}}
        ],
        "execution": {},
    }

    created = service.create_job(payload)
    persisted_service = Phase1BacktestService()

    runs = persisted_service.list_runs()
    assert any(run["job_id"] == created["job_id"] for run in runs)

    job = persisted_service.get_job(created["job_id"])
    assert job is not None
    assert job["status"] == "completed"
    assert job["result"]["selection"]["scope"] == "NIFTY50:INDEX_DATASET"


def test_ensure_backtest_schema_upgrades_legacy_backtest_runs_table(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_backtest.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE backtest_runs (
                    run_id VARCHAR(50) PRIMARY KEY,
                    status VARCHAR(20),
                    created_at DATETIME,
                    completed_at DATETIME,
                    strategy_id VARCHAR(50),
                    universe VARCHAR(50),
                    initial_capital FLOAT,
                    final_capital FLOAT,
                    total_return FLOAT,
                    sharpe_ratio FLOAT,
                    max_drawdown FLOAT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE backtest_daily_results (
                    id INTEGER PRIMARY KEY,
                    run_id VARCHAR(50),
                    date DATE,
                    strategy_id VARCHAR(50)
                )
                """
            )
        )

    ensure_backtest_schema(bind=engine)

    inspector = inspect(engine)
    run_columns = {column["name"] for column in inspector.get_columns("backtest_runs")}
    daily_columns = {column["name"] for column in inspector.get_columns("backtest_daily_results")}

    assert "name" in run_columns
    assert "request_payload" in run_columns
    assert "result_payload" in run_columns
    assert "error_message" in run_columns
    assert "universe_id" in run_columns
    assert "capital_allocated" in daily_columns
    assert "regime_tag" in daily_columns

    engine.dispose()
