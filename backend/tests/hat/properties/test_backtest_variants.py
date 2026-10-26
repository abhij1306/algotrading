"""The research-variant seams: the new exit rules and the trail start (`engine.exits`), the stop
width cap (`engine.stops`), per-run overrides (`backtest.overrides`, `RunConventions.record`), a
sweep's variants file (`runs.parse_variants`). Small examples; no run of the loop."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
import yaml

from app.hat.backtest.overrides import (
    OverrideError,
    Overrides,
    parse_policy_set,
    parse_set,
    policy_digest,
    policy_text,
    set_path,
)
from app.hat.backtest.runs import parse_variants
from app.hat.backtest.spec import BacktestError, RunConventions
from app.hat.costs import DEFAULT_SLIPPAGE_BPS
from app.hat.engine import exits
from app.hat.engine.exits import ExitView, PositionState, evaluate
from app.hat.engine.stops import SKIP_TOO_WIDE, StopInputs, plan_stop
from app.hat.engine.strategy.config import (
    ExitCloseBelowSma,
    ExitEarlyFailure,
    ExitMaxHold,
    ExitTrailingAtr,
    ExitTrailingSwingLow,
    StopLowOfDay,
)
from app.hat.engine.strategy.versions import seed_config
from app.hat.risk.policy import RiskPolicy
from tests.hat.hat_backtest_support import policy_version

D = Decimal
# entry 100, initial stop 90: 1R = 10
POS = PositionState(
    qty_original=10, qty_open=10, avg_entry=D(100), initial_stop=D(90), current_stop=D(90)
)


def _fires(rule: object, view: ExitView) -> bool:
    return bool(evaluate([rule], POS, view, "close").actions)  # type: ignore[list-item]


# --- exits ---------------------------------------------------------------------------------------


def test_early_failure_needs_the_peak_to_miss_min_r_after_days() -> None:
    rule = ExitEarlyFailure(rule="early_failure", days=5, min_r=0.5)
    assert _fires(rule, ExitView(D(101), days_held=5, peak=D(104)))  # best +0.4R
    assert not _fires(rule, ExitView(D(101), days_held=5, peak=D(105)))  # reached +0.5R
    assert not _fires(rule, ExitView(D(101), days_held=4, peak=D(101)))  # too early
    assert not _fires(rule, ExitView(D(101), days_held=9))  # no peak: never fires


def test_max_hold_sells_all_after_days() -> None:
    rule = ExitMaxHold(rule="max_hold", days=60)
    result = evaluate([rule], POS, ExitView(D(150), days_held=60), "close")
    assert [(a.kind, a.qty) for a in result.actions] == [("full", 10)]
    assert not _fires(rule, ExitView(D(150), days_held=59))


def test_atr_trail_is_under_the_peak_and_waits_for_its_start() -> None:
    rule = ExitTrailingAtr(rule="trailing_atr", days=14, multiple=3.0, start_r=2.0)
    # peak 130 (+3R), ATR 5: the level is 115
    assert _fires(rule, ExitView(D(114), peak=D(130), atr={14: D(5)}))
    assert not _fires(rule, ExitView(D(116), peak=D(130), atr={14: D(5)}))
    # peak 115 (+1.5R) is below the +2R start: the trail has not started
    assert not _fires(rule, ExitView(D(99), peak=D(115), atr={14: D(5)}))


def test_trail_start_gates_the_swing_low_and_sma_exits() -> None:
    swing = ExitTrailingSwingLow(rule="trailing_swing_low", lookback_days=10, start_r=1.0)
    sma = ExitCloseBelowSma(rule="close_below_sma", sma=20, start_r=1.0)
    below = {"swing_low": {10: D(105)}, "sma": {20: D(105)}}
    for rule in (swing, sma):
        assert not _fires(rule, ExitView(D(104), peak=D(109), **below))  # +0.9R best
        assert _fires(rule, ExitView(D(104), peak=D(110), **below))
    unset = ExitTrailingSwingLow(rule="trailing_swing_low", lookback_days=10)
    assert _fires(unset, ExitView(D(104), swing_low={10: D(105)}))  # trails from entry


def test_view_from_bars_reads_peak_and_atr() -> None:
    rule = ExitTrailingAtr(rule="trailing_atr", days=2, multiple=1.0)
    assert exits.history_sessions([rule]) == 3
    assert exits.reads_peak([rule])
    assert not exits.reads_peak([ExitTrailingSwingLow(rule="trailing_swing_low", lookback_days=5)])
    view = exits.view_from_bars(
        D(110),
        [rule],
        3,
        closes=[D(100), D(102), D(104)],
        lows=[D(98), D(100), D(101)],
        highs=[D(101), D(103), D(106)],
        entry_highs=[D(103), D(106)],
    )
    assert view.peak == D(110)  # today's price above the highs since entry
    assert view.atr == {2: (D(3) + D(5)) / 2}  # true ranges 103-100 and 106-101
    assert exits.view_from_bars(D(110), [rule], 3, [], [], []).peak is None


def test_canonical_form_unchanged_without_the_new_fields() -> None:
    config = seed_config("trend-pullback-ema")
    text = str(config.to_json())
    assert "start_r" not in text
    assert "'max_pct': None" not in text


# --- stops ---------------------------------------------------------------------------------------


def test_low_of_day_width_cap() -> None:
    inputs = StopInputs(entry=D(100), tick=D("0.05"), day_lows=(D(90),))
    assert plan_stop(StopLowOfDay(rule="low_of_day"), inputs).stop is not None
    capped = plan_stop(StopLowOfDay(rule="low_of_day", max_pct=8.0), inputs)
    assert capped.skip == SKIP_TOO_WIDE


# --- overrides -----------------------------------------------------------------------------------


def test_parse_set_values() -> None:
    assert parse_set("exits[2].at_r=3") == ("exits[2].at_r", 3)
    assert parse_set("exits[time_stop].enabled=false") == ("exits[time_stop].enabled", False)
    assert parse_set("regime.index=NIFTY 500") == ("regime.index", "NIFTY 500")
    assert parse_set('exits[+]={"rule": "max_hold", "days": 60}')[1] == {
        "rule": "max_hold",
        "days": 60,
    }
    for bad in ("exits[2].at_r", "exits[+].x=1", "Exits.x=1", "exits[2]=", "a..b=1"):
        with pytest.raises(OverrideError):
            parse_set(bad)


def test_set_path_selectors() -> None:
    doc = {"exits": [{"rule": "hard_stop"}, {"rule": "x", "n": 1}], "playbooks": [{"name": "p"}]}
    set_path(doc, "exits[x].n", 2)
    set_path(doc, "exits[0].enabled", True)
    set_path(doc, "playbooks[p].stop", {"rule": "low_of_day"})
    set_path(doc, "exits[+]", {"rule": "y"})
    assert doc["exits"] == [
        {"rule": "hard_stop", "enabled": True},
        {"rule": "x", "n": 2},
        {"rule": "y"},
    ]
    assert doc["playbooks"] == [{"name": "p", "stop": {"rule": "low_of_day"}}]
    for path in ("exits[zz].n", "exits[9].n", "missing.n", "exits.n"):
        with pytest.raises(OverrideError):
            set_path(doc, path, 1)


def test_apply_config_changes_only_the_named_values() -> None:
    config = seed_config("trend-pullback-ema")
    changed = Overrides(
        sets=(
            ("exits[trailing_swing_low].lookback_days", 20),
            ("exits[move_stop_to_breakeven].at_r", 3),
            ("exits[+]", {"rule": "early_failure", "days": 10}),
            ("playbooks[default].stop.max_pct", 12),
        )
    ).apply_config(config)
    rules = {rule.rule: rule for rule in changed.exits}
    assert rules["trailing_swing_low"].lookback_days == 20  # type: ignore[union-attr]
    assert rules["move_stop_to_breakeven"].at_r == 3  # type: ignore[union-attr]
    assert rules["early_failure"] == ExitEarlyFailure(rule="early_failure", days=10)
    assert changed.playbook("default").stop.max_pct == 12  # type: ignore[union-attr]
    assert changed.regime == config.regime
    no_hard_stop = Overrides(sets=(("exits[hard_stop].enabled", False),))
    with pytest.raises(OverrideError, match="backtest_override_invalid"):
        no_hard_stop.apply_config(config)
    assert Overrides(sets=(("bot", "other"),)).touches_identity()


def test_apply_policy_and_record() -> None:
    version = policy_version()
    overrides = Overrides(risk_pct=D("0.0050"), max_positions=10)
    changed = overrides.apply_policy(version)
    assert changed.policy.risk_pct == D("0.0050")
    assert changed.policy.max_open_positions == 10
    ladder = changed.policy.ladder
    assert [ladder[k].max_positions for k in ("L1", "L2", "L3")] == [2, 4, 10]
    fewer = Overrides(max_positions=3).apply_policy(version).policy.ladder
    assert [fewer[k].max_positions for k in ("L1", "L2", "L3")] == [2, 3, 3]
    assert changed.version_id == version.version_id
    assert overrides.record() == {"risk_pct": "0.005", "max_positions": 10}
    too_much = Overrides(risk_pct=D(2))
    with pytest.raises(OverrideError):
        too_much.apply_policy(version)


def test_flat_ladder_holds_the_cap_at_full_size() -> None:
    version = policy_version()
    policy = version.policy
    flat = Overrides(flat_ladder=True, max_positions=10, max_per_industry=3)
    changed = flat.apply_policy(version).policy
    assert {k: (r.max_positions, r.risk_mult) for k, r in changed.ladder.items()} == {
        k: (10, D(1)) for k in ("L1", "L2", "L3")
    }
    assert changed.max_positions_per_industry == 3
    # the ladder's steps, the day limits and the kill and health rules stay as written
    assert replace(
        changed, ladder=policy.ladder, max_open_positions=6, max_positions_per_industry=2
    ) == replace(policy, max_open_positions=6, max_positions_per_industry=2)
    own_cap = Overrides(flat_ladder=True).apply_policy(version).policy
    assert {r.max_positions for r in own_cap.ladder.values()} == {policy.max_open_positions}
    assert flat.record() == {"max_positions": 10, "max_per_industry": 3, "flat_ladder": True}
    flat_run = RunConventions(regime_mult="policy", overrides=Overrides(flat_ladder=True))
    with pytest.raises(BacktestError, match="backtest_promotion_with_overrides"):
        flat_run.check_promotion(DEFAULT_SLIPPAGE_BPS)


def test_policy_text_takes_a_whole_policy_and_refuses_an_invalid_one() -> None:
    policy = policy_version().policy
    as_yaml = yaml.safe_dump(policy.to_json(), sort_keys=False)
    assert policy_text(as_yaml) == policy
    assert policy_text(json.dumps(policy.to_json())) == policy
    # an unquoted decimal reads as the decimal written, never a float
    unquoted = as_yaml.replace("risk_pct: '0.0025'", "risk_pct: 0.0075")
    assert policy_text(unquoted).risk_pct == D("0.0075")
    for bad in ("risk_pct: '0.01'", "{", as_yaml.replace("zero_dd: '0.10'", "zero_dd: '0.05'")):
        with pytest.raises(OverrideError, match="backtest_policy_invalid"):
            policy_text(bad)


def test_parse_policy_set_values() -> None:
    assert parse_policy_set("health_bands.half_dd=0.12") == ("health_bands.half_dd", "0.12")
    assert parse_policy_set("max_open_positions = 8") == ("max_open_positions", 8)
    assert parse_policy_set('kill.bot_drawdown_pct="0.15"') == ("kill.bot_drawdown_pct", "0.15")
    assert parse_policy_set("p_max.momentum-breakout-ep=0.30") == (
        "p_max.momentum-breakout-ep",
        "0.30",
    )
    for bad in ("risk_pct", "risk_pct=", "kill..x=1", "=1"):
        with pytest.raises(OverrideError, match="backtest_bad_override_path"):
            parse_policy_set(bad)


def test_candidate_policy_then_fields_then_caps() -> None:
    version = policy_version()
    doc = version.policy.to_json()
    doc["global_open_risk_pct"] = "0.03"
    doc["kill"]["account_drawdown_pct"] = "0.20"
    candidate = policy_text(json.dumps(doc))
    overrides = Overrides(
        policy=candidate,
        policy_sets=(("health_bands.half_dd", "0.12"), ("health_bands.zero_dd", "0.15")),
        max_positions=10,
    )
    changed = overrides.apply_policy(version)
    assert changed.version_id == version.version_id
    got = changed.policy
    assert (got.global_open_risk_pct, got.account_drawdown_pct) == (D("0.03"), D("0.20"))
    assert (got.health_half_dd, got.health_zero_dd) == (D("0.12"), D("0.15"))
    assert got.max_open_positions == 10
    record = overrides.record()
    assert record["policy"] == {"digest": policy_digest(candidate), "document": candidate.to_json()}
    assert record["policy_set"][0] == {"path": "health_bands.half_dd", "value": "0.12"}
    assert policy_digest(candidate) != policy_digest(version.policy)
    for sets, code in (
        ((("kill.nope", "0.1"),), "backtest_bad_override_path"),
        ((("risk_pct.x", "0.1"),), "backtest_bad_override_path"),
        ((("health_bands.zero_dd", "0.05"),), "backtest_override_invalid"),
    ):
        bad_sets = Overrides(policy_sets=sets)
        with pytest.raises(OverrideError, match=code):
            bad_sets.apply_policy(version)
    for refused in (Overrides(policy=candidate), Overrides(policy_sets=sets)):
        refused_run = RunConventions(regime_mult="policy", overrides=refused)
        with pytest.raises(BacktestError, match="backtest_promotion_with_overrides"):
            refused_run.check_promotion(DEFAULT_SLIPPAGE_BPS)


def test_conventions_record_overrides_only_when_set() -> None:
    from tests.hat.hat_backtest_support import zero_fills

    fills, weight, table = zero_fills(), D("0.5"), {}
    window = (date(2025, 1, 6), date(2025, 2, 7))
    plain = RunConventions().record(fills, window, weight, table)
    assert "overrides" not in plain
    varied = RunConventions(overrides=Overrides(sets=(("exits[0].enabled", True),)))
    assert varied.record(fills, window, weight, table)["overrides"] == {
        "set": [{"path": "exits[0].enabled", "value": True}]
    }
    capped = RunConventions(regime_mult="policy", overrides=Overrides(max_positions=3))
    with pytest.raises(BacktestError, match="backtest_promotion_with_overrides"):
        capped.check_promotion(DEFAULT_SLIPPAGE_BPS)


def _sweep(*items: object) -> dict[str, object]:
    return {"sweep_id": "sw-1", "variants": list(items)}


def test_parse_variants_layers_on_the_base() -> None:
    base = RunConventions(overrides=Overrides(sets=(("exits[0].enabled", True),), risk_pct=D(1)))
    sweep = parse_variants(
        _sweep(
            {"label": "base"},
            {
                "label": "wide",
                "base": "base",
                "change": "wider target, half weight",
                "set": ["exits[1].at_r=3"],
                "risk_pct": 0.005,
                "max_positions": 8,
                "max_per_industry": 3,
                "flat_ladder": True,
                "sleeve_weight": 0.5,
                "slot_order": "score",
            },
        ),
        base,
    )
    assert (sweep.sweep_id, sweep.sweep_name) == ("sw-1", "sw-1")
    variants = sweep.variants
    assert variants[0].conventions == base
    assert (variants[1].base, variants[1].change) == ("base", "wider target, half weight")
    wide = variants[1].conventions
    assert wide.overrides == Overrides(
        sets=(("exits[0].enabled", True), ("exits[1].at_r", 3)),
        risk_pct=D("0.005"),
        max_positions=8,
        max_per_industry=3,
        flat_ladder=True,
    )
    assert (wide.sleeve_weight, wide.slot_order) == (D("0.5"), "score")
    named = parse_variants({**_sweep({"label": "a"}), "sweep_name": "named"}, base)
    assert named.sweep_name == "named"
    run_id = "019a2b3c-4d5e-7f60-8a1b-2c3d4e5f6a7b"
    assert parse_variants(_sweep({"label": "a", "base": run_id}), base).variants[0].base == run_id
    candidate = policy_version().policy
    files = {"cand.yaml": candidate}
    (pol,) = parse_variants(
        _sweep({"label": "p", "policy": "cand.yaml", "policy_set": ["kill.bot_drawdown_pct=0.15"]}),
        base,
        files.__getitem__,
    ).variants
    assert pol.conventions.overrides.policy == candidate
    assert pol.conventions.overrides.policy_sets == (("kill.bot_drawdown_pct", "0.15"),)

    def unreadable(path: str) -> RiskPolicy:
        raise OverrideError("backtest_policy_unreadable")

    sweep = _sweep({"label": "p", "policy": "x.yaml"})
    with pytest.raises(BacktestError, match="backtest_policy_unreadable"):
        parse_variants(sweep, base, unreadable)
    for bad in (
        [{"label": "a"}],  # the old list form
        {"variants": [{"label": "a"}]},  # no sweep id
        {**_sweep({"label": "a"}), "other": 1},
        _sweep(),
        _sweep({"label": "a"}, {"label": "a"}),
        _sweep({"label": "a", "other": 1}),
        _sweep({"x": 1}),
        _sweep({"label": "a", "flat_ladder": 1}),
        _sweep({"label": "a", "policy": 1}),
        _sweep({"label": "a", "policy_set": "risk_pct=0.01"}),
        _sweep({"label": "a", "base": "b"}, {"label": "b"}),  # a base not run yet
        _sweep({"label": "a", "change": "two\nlines"}),
    ):
        with pytest.raises(BacktestError):
            parse_variants(bad, base)
