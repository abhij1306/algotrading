"""The result standard's pure parts (docs/backtests/result-standard.md): the refusal and exit
codes, the buckets, the binding hash, the forward R of a refusal, the metric formulas, the trial
count and the schema check of a hand-built `run.json`. Small examples; no run of the loop."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.hat.backtest import folder, standard, trials
from app.hat.backtest.signal_features import vcp_extras
from app.hat.backtest.spec import BacktestError
from app.hat.risk.decision import Reason
from app.paths import BACKTEST_SCHEMA

D = Decimal
EXAMPLE = BACKTEST_SCHEMA.with_name("example_run.json")


# --- refusal and exit codes ----------------------------------------------------------------------


def test_every_risk_reason_has_a_refusal_code() -> None:
    assert set(Reason) <= set(standard.REFUSAL_OF)
    assert set(standard.REFUSAL_OF.values()) <= set(standard.REFUSAL_CODES)


def test_max_positions_splits_by_the_cap_that_bound() -> None:
    assert standard.refusal_code(Reason.MAX_POSITIONS, 8, 8) == "max_positions"
    assert standard.refusal_code(Reason.MAX_POSITIONS, 4, 8) == "ladder_level"
    assert standard.refusal_code(Reason.ENTRY_CAP, 4, 8) == "max_positions"
    assert standard.refusal_code(Reason.COOLDOWN, 0, 8) == "stopout_cap"
    with pytest.raises(BacktestError, match="backtest_unmapped_refusal"):
        standard.refusal_code("a_new_gate", 0, 8)


def test_exit_codes() -> None:
    assert standard.exit_code("hard_stop", D(90), D(90), ()) == "initial_stop"
    assert standard.exit_code("hard_stop", D(90), None, ()) == "initial_stop"
    assert standard.exit_code("hard_stop", D(90), D(95), ("2:step_trail",)) == "trail_step"
    assert standard.exit_code("hard_stop", D(90), D(95), ("1:trailing_atr",)) == "trailing_atr"
    assert standard.exit_code("hard_stop", D(90), D(95), ()) == "raised_stop"
    assert standard.exit_code("close_below_sma", D(90), D(90), ()) == "sma50_close"
    assert standard.exit_code("end_of_period", D(90), D(90), ()) == "period_end"
    assert standard.exit_code("demerger", D(90), D(90), ()) == "demerger"


# --- buckets -------------------------------------------------------------------------------------


def test_liquidity_quintile_within_the_day() -> None:
    universe = sorted(float(n) for n in range(1, 11))
    assert [standard.quintile(v, universe) for v in (1.0, 3.0, 5.0, 8.0, 10.0)] == [
        "q1",
        "q2",
        "q3",
        "q4",
        "q5",
    ]
    assert standard.quintile(4.0, []) == "q3"


def test_breadth_buckets() -> None:
    found = [standard.breadth_bucket(v) for v in (None, 10, 40, 49.9, 50, 60, D("75.5"))]
    assert found == [
        "breadth_unknown",
        "breadth_lt40",
        "breadth_40_50",
        "breadth_40_50",
        "breadth_50_60",
        "breadth_ge60",
        "breadth_ge60",
    ]


# --- binding -------------------------------------------------------------------------------------


def test_binding_against_the_base() -> None:
    a = ("INFY", date(2020, 1, 2), date(2020, 2, 3))
    b = ("TCS", date(2020, 1, 5), date(2020, 1, 20))
    c = ("SBIN", date(2021, 3, 1), date(2021, 4, 1))
    assert standard.trade_list_hash([a, b]) == standard.trade_list_hash([b, a])
    assert standard.binding([a, b], None) == {
        "trades_changed": None,
        "trade_list_hash": standard.trade_list_hash([a, b]),
        "no_op": None,
    }
    same = standard.binding([a, b], [b, a])
    assert (same["trades_changed"], same["no_op"]) == (0, True)
    moved = standard.binding([a, c], [a, b])
    assert (moved["trades_changed"], moved["no_op"]) == (2, False)


# --- forward R of a refusal ----------------------------------------------------------------------


def test_fwd_r() -> None:
    path = [(D(98), D(101)), (None, D(101)), (D(99), D(104))]
    assert standard.fwd_r(D(100), D(95), path, 3) == D("0.8")
    assert standard.fwd_r(D(100), D(95), path, 4) is None  # the data ends first
    assert standard.fwd_r(D(100), D(95), [(D(94), D(110)), *path], 3) == D(-1)
    assert standard.fwd_r(D(100), D(100), path, 1) is None  # no risk


# --- metrics -------------------------------------------------------------------------------------


def test_cagr_by_calendar_days() -> None:
    assert standard.cagr_pct(D(121), D(100), 731) == pytest.approx(10.0, abs=0.01)
    assert standard.cagr_pct(D(0), D(100), 365) is None
    assert standard.cagr_pct(D(110), D(100), 0) is None


def test_sharpe_and_sortino() -> None:
    returns = [0.01, -0.005, 0.02, -0.01, 0.0]
    mean = sum(returns) / 5
    sd = (sum((r - mean) ** 2 for r in returns) / 5) ** 0.5
    down = (sum(min(r, 0.0) ** 2 for r in returns) / 5) ** 0.5
    assert standard.sharpe(returns) == pytest.approx(mean / sd * 252**0.5)
    assert standard.sortino(returns) == pytest.approx(mean / down * 252**0.5)
    assert standard.sortino([0.01, 0.02]) is None
    assert standard.sharpe([0.01]) is None


def test_drawdowns_with_dates_and_recovery() -> None:
    days = [date(2020, 1, 1) + timedelta(days=n) for n in range(7)]
    curve = list(zip(days, map(D, (110, 99, 110, 120, 90, 96, 100)), strict=True))
    found = standard.drawdowns(curve, D(100))
    assert found.max_pct == pytest.approx(25.0)
    assert found.avg_pct == pytest.approx(17.5)
    assert (found.longest_sessions, found.longest_start) == (3, days[3])
    assert (found.longest_end, found.longest_recovered) == (days[6], False)
    flat = standard.drawdowns(curve[:1], D(100))
    assert (flat.max_pct, flat.avg_pct, flat.longest_recovered) == (0.0, None, None)


def test_losses_and_concentration() -> None:
    rs = [D(1), D(-1), D(0), D(-1), D(2), D(-1)]
    assert standard.max_consecutive_losses(rs) == 3
    pnls = [D(50), D(-20), D(30), D(10)]
    assert standard.top_share_pct(pnls, 2, D(70)) == pytest.approx(80 / 70 * 100)
    assert standard.top_share_pct(pnls, 2, D(-1)) is None


def test_changed_paths() -> None:
    base = {"exits": [{"at_r": 2}, {"days": 10}], "risk": {"pct": 0.01}}
    mine = {"exits": [{"at_r": 3}, {"days": 10}], "risk": {"pct": 0.01, "cap": 5}}
    assert standard.changed_paths(mine, base) == {"exits.0.at_r": 3, "risk.cap": 5}


# --- trials --------------------------------------------------------------------------------------


def test_trial_count_is_distinct_hashes(tmp_path: Path) -> None:
    assert trials.family({}, "S1", ("h1", 0.5)) == (1, [0.5])
    trials.record_trial(tmp_path, "S1", "h1", "run-a", 0.5)
    trials.record_trial(tmp_path, "S1", "h1", "run-b", 0.9)  # a duplicate keeps its first run
    trials.record_trial(tmp_path, "S1", "h2", "run-c", None)
    trials.record_trial(tmp_path, "S2", "h1", "run-d", 0.1)
    registry = trials.read_registry(tmp_path)
    assert registry["S1"]["h1"] == trials.Trial("run-a", 0.5)
    assert trials.family(registry, "S1", ("h1", 0.7)) == (2, [0.5])
    assert trials.family(registry, "S1", ("h3", 0.7)) == (3, [0.5, 0.7])
    assert trials.family(registry, "S1", None) == (2, [0.5])


# --- the run folder ------------------------------------------------------------------------------


def test_example_run_is_valid_and_a_broken_one_is_not() -> None:
    doc = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert folder.validate(doc) == []
    broken = {**doc, "refusals": {"no_such_gate": 1}}
    assert folder.validate(broken)
    assert folder.validate({**doc, "format_version": "1.0"})


def test_failed_run_is_valid() -> None:
    example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    identity = {**example["identity"], "status": "failed", "error": "backtest_no_sessions"}
    inputs = folder.RunInputs(
        identity=identity,
        data=example["data"],
        config=example["config"],
        start_capital=D(1000000),
        policy_max_positions=8,
    )
    assert folder.validate(folder.failed_run(inputs)) == []


def test_run_ids_never_leave_the_runs_folder(tmp_path: Path) -> None:
    assert folder.run_dir(tmp_path, "019a2b3c-4d5e-7f60-8a1b-2c3d4e5f6a7b") is not None
    for bad in ("../trials", "019a2b3c", "", "019A2B3C-4D5E-7F60-8A1B-2C3D4E5F6A7B"):
        assert folder.run_dir(tmp_path, bad) is None
    assert folder.read_csv(tmp_path, "019a2b3c-4d5e-7f60-8a1b-2c3d4e5f6a7b", "config") is None


def test_vcp_extras_from_the_criteria_results() -> None:
    extras = {"contractions": 3, "base_length": 40}
    criteria = {
        "stages": [
            {"criteria": [{"type": "trend", "extras": {"x": 1}}]},
            {"criteria": [{"type": "vcp", "extras": extras}]},
        ]
    }
    assert vcp_extras(criteria) == extras
    assert vcp_extras({"stages": [{"criteria": [{"type": "vcp"}]}]}) is None
    assert vcp_extras(None) is None
