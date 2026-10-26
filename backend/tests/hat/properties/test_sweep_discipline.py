"""Sweep discipline (`app.hat.backtest.discipline`): one change per variant, the hold-out guards,
and the run audit's `one_change` and `policy_drift` findings."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.hat.backtest import compare, run_audit
from app.hat.backtest.discipline import (
    candidate_hash,
    changed_knobs,
    check_hold_out,
    knobs_of,
    resolve_knobs,
)
from app.hat.backtest.overrides import Overrides
from app.hat.backtest.runs import check_one_change, parse_variants
from app.hat.backtest.spec import BacktestError, RunConventions

D = Decimal
BASE = RunConventions()
SETS = ["exits[time_stop].enabled=false", "exits[close_below_sma].sma=50"]
CONFIG = {
    "exits": [
        {"rule": "time_stop", "enabled": True},
        {"rule": "close_below_sma", "sma": 20},
    ]
}


def _sweep(*items: object) -> dict[str, object]:
    return {"sweep_id": "sw-1", "variants": [{"label": "base"}, *items]}


def _checked(*items: object) -> object:
    sweep = parse_variants(_sweep(*items), BASE)
    check_one_change(sweep, CONFIG)
    return sweep


def test_knobs_name_each_path_and_convention() -> None:
    conventions = RunConventions(
        slot_order="score",
        overrides=Overrides(sets=(("a.b", 1), ("a.b", 2), ("c", [2, 6])), risk_pct=D("0.0050")),
    )
    knobs = knobs_of(conventions, D(20), "cnc", None)
    assert (knobs["set:a.b"], knobs["set:c"], knobs["risk_pct"]) == ("2", "[2,6]", "0.005")
    plain = knobs_of(BASE, D(20), "cnc", None)
    assert changed_knobs(knobs, plain) == ["risk_pct", "set:a.b", "set:c", "slot_order"]
    assert changed_knobs(knobs_of(BASE, D("20.0"), "cnc", 5), plain) == ["cooldown_sessions"]


def test_knobs_are_named_by_the_resolved_path() -> None:
    by_name = resolve_knobs({"set:exits[time_stop].enabled": "false"}, CONFIG)
    by_index = resolve_knobs({"set:exits[0].enabled": "false"}, CONFIG)
    assert by_name == by_index == {"set:exits[0].enabled": "false"}
    # an append is named by the new item's index; a path naming nothing is kept as written
    knobs = {
        "set:exits[+]": '{"rule":"max_hold"}',
        "set:exits[max_hold].days": "60",
        "set:x.y": "1",
    }
    assert list(resolve_knobs(knobs, CONFIG)) == ["set:exits[2]", "set:exits[2].days", "set:x.y"]
    assert resolve_knobs({"risk_pct": "0.005"}, CONFIG) == {"risk_pct": "0.005"}


def test_one_change_per_variant_unless_a_declared_bundle() -> None:
    one = {"label": "one", "base": "base", "set": SETS[:1]}
    assert _checked(one).variants[1].bundle is None
    two = {"label": "two", "base": "base", "set": SETS}
    with pytest.raises(BacktestError, match="backtest_sweep_not_one_change"):
        _checked(two)
    with pytest.raises(BacktestError, match="backtest_sweep_not_one_change"):
        _checked({**one, "slot_order": "score"})
    bundled = _checked({**two, "bundle": True, "reason": "one exit set"})
    assert bundled.variants[1].bundle == "one exit set"
    # the change is counted against the variant's own base, not the file's first variant
    chained = {"label": "next", "base": "one", "set": SETS}
    assert _checked(one, chained).variants[2].base == "one"
    # the base's path spelt by index is the same knob: one change, not three
    respelt = {"label": "next", "base": "one", "set": ["exits[0].enabled=false", SETS[1]]}
    _checked(one, respelt)
    for bad in (
        {**two, "bundle": True},  # no reason
        {**two, "reason": "why"},  # a reason without the bundle
        {**two, "bundle": "yes", "reason": "why"},
        {"label": "x", "bundle": True, "reason": "why"},  # a bundle without a base
    ):
        with pytest.raises(BacktestError, match="backtest_sweep_bad_variants"):
            parse_variants(_sweep(bad), BASE)


RESOLVED = {
    "strategy": {"exits": []},
    "risk_policy": {"risk_pct": "0.005"},
    "conventions": {"cost_model": {"version": "cnc", "schedules": [{"digest": "a"}]}},
}
CANDIDATE = "01a17714-351d-73ca-bc03-df68ff425d83"


def test_candidate_hash_ignores_the_windows_cost_schedules() -> None:
    later = {**RESOLVED, "conventions": {"cost_model": {"version": "cnc", "schedules": []}}}
    assert candidate_hash(later) == candidate_hash(RESOLVED)
    other = {**RESOLVED, "risk_policy": {"risk_pct": "0.01"}}
    assert candidate_hash(other) != candidate_hash(RESOLVED)


def _publish(root: Path, run_id: str, split: str, status: str = "complete") -> None:
    out = root / "backtests" / "runs" / run_id
    out.mkdir(parents=True)
    doc = {
        "identity": {"run_id": run_id, "status": status},
        "data": {"split": split, "period_end": "2021-12-31"},
        "config": {"resolved": RESOLVED},
    }
    (out / "run.json").write_text(json.dumps(doc), encoding="utf-8")


def test_hold_out_guards(tmp_path: Path) -> None:
    start = date(2022, 1, 1)
    with pytest.raises(BacktestError, match="backtest_hold_out_unknown_candidate"):
        check_hold_out(tmp_path, CANDIDATE, RESOLVED, start)
    _publish(tmp_path, CANDIDATE, "in_sample")
    check_hold_out(tmp_path, CANDIDATE, RESOLVED, start)
    with pytest.raises(BacktestError, match="backtest_hold_out_overlaps"):
        check_hold_out(tmp_path, CANDIDATE, RESOLVED, date(2021, 12, 31))
    with pytest.raises(BacktestError, match="backtest_hold_out_config_differs"):
        check_hold_out(tmp_path, CANDIDATE, {**RESOLVED, "strategy": {}}, start)
    _publish(tmp_path, "01a17800-0000-7000-8000-000000000001", "hold_out", "failed")
    check_hold_out(tmp_path, CANDIDATE, RESOLVED, start)  # a failed hold-out was not seen
    held = "01a17800-0000-7000-8000-000000000002"
    _publish(tmp_path, held, "hold_out")
    with pytest.raises(BacktestError, match="backtest_hold_out_repeated"):
        check_hold_out(tmp_path, CANDIDATE, RESOLVED, start)
    with pytest.raises(BacktestError, match="backtest_hold_out_bad_candidate"):
        check_hold_out(tmp_path, held, RESOLVED, start)


def _binding(identity: dict, config: dict) -> dict:
    run = {
        "identity": {"base_run_id": "b", **identity},
        "config": config,
        "binding": {"trade_list_hash": "h", "no_op": False},
    }
    return run_audit.check_binding(run)


def test_binding_counts_the_changes_and_the_policy() -> None:
    one = _binding({"changed_knobs": ["set:a"]}, {})
    assert one["status"] == "pass"
    assert one["summary"]["changed"] == 1
    bundle = _binding({"changed_knobs": ["set:a", "set:b"], "bundle": "why"}, {})
    (finding,) = bundle["findings"]
    assert (finding["code"], finding["count"], finding["bundle"]) == ("one_change", 2, "why")
    # a base that predates knobs: the resolved config's changed paths
    old = _binding({}, {"overrides": {"x.1": 1, "x.2": 2}})
    assert old["findings"][0]["basis"] == "resolved_paths"
    drift = _binding(
        {"changed_knobs": []}, {"risk_policy_version": 3, "base_risk_policy_version": 2}
    )
    assert [f["code"] for f in drift["findings"]] == ["policy_drift"]


def test_compare_shows_knobs_and_policy() -> None:
    run = {"identity": {"base_run_id": "b", "changed_knobs": ["a", "b"], "bundle": "why"}}
    row = compare.row_of(run, None, (3, 2))
    assert row[-3:-1] == ["2b", "3!2"]
    assert compare.row_of({"identity": {}}, None, (3, None))[-3:-1] == ["-", "3"]
