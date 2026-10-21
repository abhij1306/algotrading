"""The copies HAT took from legacy code stay faithful while the legacy code exists (K1).

HAT's PR 6 and PR 12 characterization tests read `tests/hat/fixtures/vcp_oracle.json` and use
`app.hat.backtest.metrics` instead of the legacy VCP detector and `backtest_metrics`. These checks
compare each copy with its source. They are legacy tests: delete this file with the VCP detector
and `services/backtest_metrics.py` (K5).
"""

import dataclasses
import json
from pathlib import Path

import pandas as pd
import pytest

from app.hat.backtest import metrics as hat_metrics
from app.services import backtest_metrics as legacy_metrics
from app.services.vcp import pattern

ORACLE = json.loads(
    (Path(__file__).parent / "hat" / "fixtures" / "vcp_oracle.json").read_text(encoding="utf-8")
)


def test_oracle_constants_match_the_detector() -> None:
    assert ORACLE["constants"] == {
        "min_history_bars": pattern.MIN_HISTORY_BARS,
        "min_rs_rating": pattern.MIN_RS_RATING,
        "stop_offset_pct": pattern.STOP_OFFSET_PCT,
    }


@pytest.mark.parametrize("case", ORACLE["cases"], ids=lambda c: c["name"])
def test_oracle_case_matches_the_detector(case: dict) -> None:
    result = pattern.analyze_pattern(pd.DataFrame(case["bars"]), case["rs_rating"])
    got = None if result is None else json.loads(json.dumps(dataclasses.asdict(result)))
    assert got == case["expected"]


def test_oracle_rs_map_matches_the_detector() -> None:
    frames = {
        name: pd.DataFrame({"close_ref": spec["close_ref"]})
        for name, spec in ORACLE["rs_map"]["symbols"].items()
    }
    assert pattern.compute_rs_map(frames) == ORACLE["rs_map"]["expected"]


def test_hat_metrics_equal_the_legacy_ones_on_ordinary_series() -> None:
    idx = pd.date_range("2025-01-01", periods=6)
    ret = pd.Series([0.0, 0.02, -0.01, 0.03, -0.02, 0.01], index=idx)
    equity = 100000 * (1 + ret).cumprod()
    assert hat_metrics.drawdown_series(equity).equals(legacy_metrics.drawdown_series(equity))
    assert hat_metrics.sharpe_mean_std(ret) == legacy_metrics.sharpe_mean_std(ret)
    assert hat_metrics.sharpe_excess_cagr(0.2, ret, 0.05) == legacy_metrics.sharpe_excess_cagr(
        0.2, ret, 0.05
    )
    assert hat_metrics.annualized_return(1.2e5, 1e5, 126) == legacy_metrics.annualized_return(
        1.2e5, 1e5, 126
    )
    values = [5.0, -2.0, 3.5, 0.0]
    assert dataclasses.asdict(hat_metrics.trade_stats(values)) == dataclasses.asdict(
        legacy_metrics.trade_stats(values)
    )
