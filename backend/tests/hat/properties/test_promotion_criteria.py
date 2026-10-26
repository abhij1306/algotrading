"""The promotion criteria block (`backtest/criteria.py`): parsing, its refusals, and each row on
small hand-built `run.json` documents. No run folder, no registry."""

from __future__ import annotations

import pytest

from app.hat.backtest import criteria as crit

BLOCK = """strategy: S1
universe: U
in_sample: {from: 2016-01-01, to: 2021-12-31}
hold_out: {from: 2022-01-01, to: 2026-10-23}
trial_budget: 30
checks:
  mar_min: 0.6
  longest_dd_sessions_below: 500
  trades_min: 100
  cagr_above_benchmark: true
  cagr_ex_top5_min_share: 0.5
  max_pct_adv: 5
  binding_required: true
plateau:
  mar_min: 0.5
  parameters:
    - {path: strategy.exits.4.lookback_days, step: 5}
    - {path: strategy.exits.2.enabled}
hold_out_checks: {expectancy_min_share: 0.5, mar_min: 0.4}
"""


def _run(run_id: str = "r", **metrics: object) -> dict:
    return {
        "identity": {"run_id": run_id, "status": "complete", "strategy": "S1"},
        "data": {"universe": "U", "period_start": "2016-01-01", "period_end": "2021-12-31"},
        "config": {
            "resolved": {
                "strategy": {"exits": [{}, {}, {"enabled": False}, {}, {"lookback_days": 20}]}
            }
        },
        "binding": {"no_op": False},
        "metrics": {
            "mar": 0.7,
            "longest_dd_sessions": 400,
            "trades": 120,
            "cagr_pct": 20.0,
            "benchmark_cagr_pct": 15.0,
            "cagr_ex_top5_trades_pct": 12.0,
            "expectancy_r": 0.4,
        }
        | metrics,
    }


TRADES = [{"leg": "1", "position_pct_adv": "0.3"}, {"leg": "2", "position_pct_adv": "9"}]


def test_extract_and_parse() -> None:
    c = crit.parse(crit.extract(f"# notes\n```hat-criteria\n{BLOCK}```\nmore\n"))
    assert (c.strategy, c.universe, c.in_sample, c.trial_budget) == (
        "S1",
        "U",
        ("2016-01-01", "2021-12-31"),
        30,
    )
    assert c.parameters[1] == crit.Parameter("strategy.exits.2.enabled", None)
    assert c.digest == crit.parse(BLOCK).digest != crit.parse(BLOCK + "\n").digest
    with pytest.raises(ValueError, match="criteria_block_missing"):
        crit.extract("```yaml\nx: 1\n```\n")
    with pytest.raises(ValueError, match="criteria_block_many"):
        crit.extract(f"```hat-criteria\n{BLOCK}```\n```hat-criteria\n{BLOCK}```\n")
    for old, new, code in (
        ("trades_min", "trade_min", "criteria_bad_checks"),
        ("to: 2021-12-31", "to: 2015-12-31", "criteria_bad_in_sample"),
        ("binding_required: true", "binding_required: 1", "criteria_bad_binding_required"),
    ):
        bad = BLOCK.replace(old, new)
        with pytest.raises(ValueError, match=code):
            crit.parse(bad)


def test_in_sample_rows() -> None:
    c = crit.parse(BLOCK)
    good = crit.in_sample_rows(c, _run(), TRADES)
    assert {r.name: r.status for r in good if r.status != crit.PASS} == {}
    # np of s1-baseline-01: fails MAR, longest DD, CAGR vs TRI and CAGR ex top-5
    np_run = _run(
        mar=0.5593,
        longest_dd_sessions=740,
        trades=144,
        cagr_pct=9.2324,
        benchmark_cagr_pct=15.6588,
        cagr_ex_top5_trades_pct=3.287,
    )
    failed = {r.name for r in crit.in_sample_rows(c, np_run, TRADES) if r.status == crit.FAIL}
    assert failed == {
        "mar",
        "longest_dd_sessions",
        "cagr_pct vs benchmark",
        "cagr_ex_top5_trades_pct",
    }
    other = _run()
    other["identity"]["strategy"] = "S2"
    assert [r.status for r in crit.in_sample_rows(c, other, TRADES)][0] == crit.FAIL  # scope
    partly = [*TRADES, {"leg": "1", "position_pct_adv": ""}]  # one entry never measured
    adv = {r.name: r for r in crit.in_sample_rows(c, _run(), partly)}["max position % ADV"]
    assert (adv.status, adv.note) == (crit.FAIL, "1 entries without ADV")
    big = crit.in_sample_rows(c, _run(), [{"leg": "1", "position_pct_adv": "6"}])
    assert {r.name for r in big if r.status == crit.FAIL} == {"max position % ADV"}
    off = _run()
    off["data"]["period_end"] = "2020-12-31"
    off["binding"]["no_op"] = None
    assert {r.name for r in crit.in_sample_rows(c, off, TRADES) if r.status == crit.FAIL} == {
        "scope",
        "binding.no_op",
    }


def test_trial_budget_counts_own_trials_only() -> None:
    c = crit.parse(BLOCK)
    (row,) = crit.trial_rows(c, _run(dsr_n_trials=56), "S1/U/..", total=56, prior=54)
    assert (row.value, row.status) == (2, crit.PASS)
    (over,) = crit.trial_rows(c, _run(), "S1/U/..", total=85, prior=54)
    assert over.status == crit.FAIL


def _neighbour(run_id: str, path: tuple[int, str], value: object, mar: float) -> dict:
    run = _run(run_id, mar=mar)
    run["config"]["resolved"]["strategy"]["exits"][path[0]] = {path[1]: value}
    return run


def test_plateau_reads_one_step_either_side() -> None:
    c = crit.parse(BLOCK)
    run = _run()
    assert [r.status for r in crit.plateau_rows(c, run, None, [])] == [crit.NOT_RUN]
    sweep = [
        _neighbour("a", (4, "lookback_days"), 15, 0.55),
        _neighbour("b", (4, "lookback_days"), 25, 0.45),
        _neighbour("c", (4, "lookback_days"), 30, 0.10),  # two steps: not a neighbour
        _neighbour("d", (2, "enabled"), True, 0.6),
    ]
    rows = {r.name: r.status for r in crit.plateau_rows(c, run, "p", sweep)}
    assert rows == {
        "strategy.exits.4.lookback_days -5": crit.PASS,
        "strategy.exits.4.lookback_days +5": crit.FAIL,
        "strategy.exits.2.enabled flip": crit.PASS,
    }
    partial = {r.name: r.status for r in crit.plateau_rows(c, run, "p", sweep[:1])}
    assert partial["strategy.exits.4.lookback_days +5"] == crit.NOT_RUN
    assert crit.overall(crit.plateau_rows(c, run, "p", sweep[:1])) == "incomplete"


def test_hold_out_rows() -> None:
    c = crit.parse(BLOCK)
    assert [r.status for r in crit.hold_out_rows(c, _run(), None, 0)] == [crit.NOT_RUN]
    hold = _run("h", expectancy_r=0.15, mar=0.5)
    hold["data"].update(period_start="2022-01-01", period_end="2026-10-23")
    rows = {r.name: r.status for r in crit.hold_out_rows(c, _run(), hold, 1)}
    assert rows == {
        "scope": crit.PASS,
        "hold-out reads": crit.PASS,
        "expectancy_r": crit.FAIL,  # 0.15 < 0.5 x 0.4
        "mar": crit.PASS,
    }
    twice = {r.name: r.status for r in crit.hold_out_rows(c, _run(), hold, 2)}
    assert twice["hold-out reads"] == crit.FAIL


def test_dsr_and_pbo_are_read_as_of_today() -> None:
    c = crit.parse(BLOCK.replace("binding_required: true", "dsr_prob_min: 0.9\n  pbo_max: 0.5"))
    stale = _run(dsr_prob=0.99, pbo=0.0)  # scored at completion, before later trials
    rows = {r.name: r.status for r in crit.in_sample_rows(c, stale, TRADES)}
    assert (rows["dsr_prob now"], rows["pbo now"]) == (crit.FAIL, crit.FAIL)
    now = {"dsr_prob": 0.002, "pbo": 0.2}
    rows = {r.name: r.status for r in crit.in_sample_rows(c, stale, TRADES, now)}
    assert (rows["dsr_prob now"], rows["pbo now"]) == (crit.FAIL, crit.PASS)


def test_a_failed_or_missing_audit_fails() -> None:
    rows = crit.audit_rows("in-sample", "warn", "pass")
    assert [r.status for r in rows] == [crit.PASS, crit.PASS]
    assert [r.status for r in crit.audit_rows("in-sample", "fail", None)] == [crit.FAIL] * 2
