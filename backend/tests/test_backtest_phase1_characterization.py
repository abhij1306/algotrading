"""Golden-master tests for Phase1BacktestService.run, pinned ahead of a refactor.

tests/fixtures/phase1_backtest_golden.json is the output of run() for the inputs
below, captured before the refactor.
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from app.services.backtest_phase1_service import Phase1BacktestService

GOLDEN = Path(__file__).parent / "fixtures" / "phase1_backtest_golden.json"
DATES = [date(2025, 1, d) for d in range(1, 7)]
PRICES = {
    "AAA": [100.0, 102.0, 101.0, 105.0, 104.0, 108.0],
    "BBB": [50.0, 49.0, 51.0, 50.5, 52.0, 51.0],
}
INDEX = [1000.0, 1010.0, 1005.0, 1030.0, 1025.0, 1050.0]
STRATEGIES = [
    {"strategy_id": "MOMENTUM_2D", "weight": 0.6, "params": {"lookback_days": 1}},
    {"strategy_id": "MEAN_REVERSION_3D", "weight": 0.4, "params": {"lookback_days": 1}},
]
BASE = {"start_date": "2025-01-01", "end_date": "2025-01-06", "initial_capital": 100000}


@pytest.fixture
def service() -> Phase1BacktestService:
    svc = Phase1BacktestService()
    snapshot = pd.DataFrame(
        [
            {"date": d, "symbol": s, "price": p}
            for s, ps in PRICES.items()
            for d, p in zip(DATES, ps, strict=True)
        ]
    )
    index = pd.DataFrame({"date": DATES, "symbol": ["NIFTY50"] * 6, "price": INDEX})
    svc._load_symbol_snapshot = lambda _symbols: snapshot.copy()  # type: ignore[method-assign]
    svc._load_universe_index_dataset = lambda *_args: index.copy()  # type: ignore[method-assign]
    return svc


def as_json(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, default=str))


def test_symbols_and_universe_runs_match_golden_output(service: Phase1BacktestService) -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    symbols = service.run(
        {
            **BASE,
            "selection": {"mode": "symbols", "symbols": ["AAA", "BBB"]},
            "strategies": STRATEGIES,
        }
    )
    universe = service.run(
        {**BASE, "selection": {"mode": "universe"}, "strategies": STRATEGIES[:1]}
    )

    assert as_json(symbols) == golden["symbols"]
    assert as_json(universe) == golden["universe"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"instrument_type": "futures"}, "Unsupported instrument_type='futures'"),
        (
            {"instrument_type": "options"},
            "Options backtest is not available until options historical dataset is onboarded",
        ),
        (
            {"start_date": "2025-01-06", "end_date": "2025-01-01"},
            "start_date cannot be after end_date",
        ),
        ({"initial_capital": 0}, "initial_capital must be > 0"),
        ({"selection": {"mode": "symbols", "symbols": "AAA"}}, "selection.symbols must be a list"),
        ({"selection": {"mode": "sector"}}, "selection.mode must be 'universe' or 'symbols'"),
        (
            {"end_date": "2025-01-09"},
            "Requested range outside available data: 2025-01-01 to 2025-01-06",
        ),
        ({"strategies": []}, "At least one strategy must be provided"),
        ({"strategies": {"strategy_id": "MOMENTUM_2D"}}, "At least one strategy must be provided"),
    ],
)
def test_run_rejects_invalid_requests(
    service: Phase1BacktestService, overrides: dict[str, Any], message: str
) -> None:
    payload = {
        **BASE,
        "selection": {"mode": "symbols", "symbols": ["AAA", "BBB"]},
        "strategies": STRATEGIES,
        **overrides,
    }

    with pytest.raises(ValueError) as excinfo:
        service.run(payload)

    assert str(excinfo.value) == message
