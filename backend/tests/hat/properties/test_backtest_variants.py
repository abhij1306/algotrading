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
from app.hat.backtest.runs import check_paths, parse_variants
from app.hat.backtest.signal_features import setup_tag_columns
from app.hat.backtest.spec import BacktestError, RunConventions
from app.hat.backtest.standard import exit_code
from app.hat.costs import DEFAULT_SLIPPAGE_BPS
from app.hat.engine import exits
from app.hat.engine.exits import ExitView, PositionState, evaluate
from app.hat.engine.stops import SKIP_TOO_WIDE, StopInputs, plan_stop
from app.hat.engine.strategy.config import (
    ExitCloseBelowSma,
    ExitEarlyFailure,
    ExitMaxHold,
    ExitTennisBall,
    ExitTopSignal,
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


def _flat(n: int, close: str = "100") -> list[Decimal]:
    return [D(close)] * n


TOP = ExitTopSignal(rule="top_signal", lookback=5, stage4=False)


def test_top_signal_needs_the_largest_decline_on_the_largest_volume() -> None:
    closes = [D(100), D(99), D(100), D(98), D(100), D(95)]  # -5%: the largest of 5 changes
    assert exits.top_signal_sessions([TOP]) == 6
    assert exits.top_signal_seen([TOP], closes, [1.0, 2.0, 3.0, 2.0, 9.0])
    assert not exits.top_signal_seen([TOP], closes, [1.0, 2.0, 9.0, 2.0, 9.0])  # a tie: no
    assert not exits.top_signal_seen([TOP], closes, [1.0, 2.0, 3.0, 2.0, float("nan")])
    milder = [*closes[:-1], D(99)]  # -1%: the -2% before it is larger
    assert not exits.top_signal_seen([TOP], milder, [1.0, 2.0, 3.0, 2.0, 9.0])
    assert not exits.top_signal_seen([TOP], closes[1:], [1.0, 2.0, 3.0, 2.0, 9.0])  # too short
    off = TOP.model_copy(update={"enabled": False})
    assert not exits.top_signal_seen([off], closes, [1.0, 2.0, 3.0, 2.0, 9.0])
    assert exits.top_signal_sessions([off]) == 0


def test_top_signal_stage4_is_a_close_under_a_falling_sma200() -> None:
    rule = TOP.model_copy(update={"stage4": True})
    assert exits.top_signal_sessions([rule]) == exits.STAGE4_SESSIONS
    falling = [D(200 - n) / 2 for n in range(221)]  # each close lower: under a falling SMA200
    volumes = [1.0] * 221  # no volume record: only stage 4 can signal
    assert exits.top_signal_seen([rule], falling, volumes)
    assert not exits.top_signal_seen([TOP], falling, volumes)  # stage4 off
    assert not exits.top_signal_seen([rule], falling[1:], volumes)  # no SMA200 21 back
    rising = list(reversed(falling))
    assert not exits.top_signal_seen([rule], rising, volumes)


def test_top_signal_sells_everything_at_the_nightly_evaluation_only() -> None:
    view = ExitView(D(95), top_signal=True)
    result = evaluate([TOP], POS, view, "nightly")
    assert [(a.kind, a.qty) for a in result.actions] == [("full", 10)]
    assert not evaluate([TOP], POS, view, "close").actions
    assert not evaluate([TOP], POS, ExitView(D(95)), "nightly").actions


BALL = ExitTennisBall(rule="tennis_ball")


def test_tennis_ball_counts_sessions_since_the_highest_close() -> None:
    assert exits.reads_peak([BALL])
    closes = [D(100), D(112), *_flat(9, "108")]  # the high close 112, then 9 lower
    view = exits.view_from_bars(D(109), [BALL], 11, [], [], [], [D(113)] * 11, closes)
    assert view.since_close_high == 10
    tie = exits.view_from_bars(D(112), [BALL], 11, [], [], [], [D(113)] * 11, closes)
    assert tie.since_close_high == 10  # a tie is no new high (no reset to 0)
    new = exits.view_from_bars(D(113), [BALL], 11, [], [], [], [D(113)] * 11, closes)
    assert new.since_close_high == 0
    assert exits.view_from_bars(D(109), [BALL], 11, [], [], []).since_close_high is None


def test_tennis_ball_moves_the_stop_to_the_entry_after_one_r() -> None:
    due = ExitView(D(104), peak=D(110), since_close_high=10)  # +1R reached, 10 sessions
    result = evaluate([BALL], POS, due, "close")
    assert (result.new_stop, result.fired, result.actions) == (D(100), ("0:tennis_ball",), ())
    assert evaluate([BALL], POS, due, "live").new_stop is None  # a close-phase rule
    assert evaluate([BALL], POS, replace(due, peak=D(109)), "close").new_stop is None  # 0.9R
    assert evaluate([BALL], POS, replace(due, since_close_high=9), "close").new_stop is None
    above = replace(POS, current_stop=D(101))
    kept = evaluate([BALL], above, due, "close")
    assert kept.new_stop is None
    assert kept.ignored == (("0:tennis_ball", "would_not_raise_stop"),)
    fired = exits.apply(POS, result)
    assert evaluate([BALL], fired, due, "close").fired == ()  # fires once


def test_a_stop_exit_after_the_tennis_ball_is_its_own() -> None:
    assert exit_code("hard_stop", D(90), D(100), ["2:tennis_ball"]) == "tennis_ball_stop"
    assert exit_code("top_signal", D(90), D(100), ["3:top_signal"]) == "top_signal"


def test_setup_tag_columns_read_the_setup_summary() -> None:
    results = {
        "setup": {
            "setup_type": "cheat_3c",
            "setup_tags": ["cheat_3c", "cup_handle"],
            "shakeout": True,
            "shakeout_undercut_pct": 2.345678,
            "ath_pivot": False,
            "early_leader": None,
            "stage": "stage_2",
        }
    }
    assert setup_tag_columns(results) == {
        "setup_type": "cheat_3c",
        "setup_tags": "cheat_3c/cup_handle",
        "tag_shakeout": True,
        "tag_undercut_pct": 2.3457,
        "tag_ath_pivot": False,
        "tag_early_leader": None,
        "tag_stage": "stage_2",
    }
    alone = setup_tag_columns({"setup": {"setup_type": "power_play"}})
    assert alone["setup_tags"] == "power_play"
    assert set(setup_tag_columns({}).values()) == {None}


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


def test_an_appended_criterion_takes_the_order_one_set_moves() -> None:
    # the appended book_vcp takes vcp's old order, and sorts into vcp's old index
    criteria = "playbooks[default].setup.criteria"
    config = Overrides(
        sets=(
            (f"{criteria}[vcp].order", 20),
            (f"{criteria}[+]", {"type": "book_vcp", "stage": "setup", "mode": "hard", "order": 11}),
        )
    ).apply_config(seed_config("stage2-contraction-breakout"))
    orders = {c.type: c.order for c in config.playbook("default").setup.criteria}
    assert (orders["book_vcp"], orders["vcp"]) == (11, 20)


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
        policy_sets=(("adv_cap_pct", "0.02"), ("max_entries_per_day", 5)),
        max_positions=10,
    )
    changed = overrides.apply_policy(version)
    assert changed.version_id == version.version_id
    got = changed.policy
    assert (got.global_open_risk_pct, got.account_drawdown_pct) == (D("0.03"), D("0.20"))
    assert (got.adv_cap_pct, got.max_entries_per_day) == (D("0.02"), 5)
    assert got.max_open_positions == 10
    # `base_weight` is read (a sleeve's weight unless the run names one): a set on it applies
    weighted = Overrides(policy_sets=(("base_weight.S1", "0.40"),))
    assert weighted.apply_policy(version).policy.base_weight["S1"] == D("0.40")
    # a playbook's key is a policy path segment as it is (hyphens included)
    p_max = Overrides(policy_sets=(("p_max.stage2-contraction-breakout", "1"),))
    assert p_max.apply_policy(version).policy.to_json()["p_max"]["stage2-contraction-breakout"]
    record = overrides.record()
    assert record["policy"] == {"digest": policy_digest(candidate), "document": candidate.to_json()}
    assert record["policy_set"][0] == {"path": "adv_cap_pct", "value": "0.02"}
    assert policy_digest(candidate) != policy_digest(version.policy)
    for sets, code in (
        ((("adv_cap_pct.x", "0.1"),), "backtest_bad_override_path"),
        ((("risk_pct.x", "0.1"),), "backtest_bad_override_path"),
        ((("max_entries_per_day", 0),), "backtest_override_invalid"),
        # a field no backtest reads changes nothing
        ((("kill.account_drawdown_pct", "0.1"),), "backtest_override_no_effect"),
        ((("health_bands.half_dd", "0.12"),), "backtest_override_no_effect"),
        ((("weights_confirmed", True),), "backtest_override_no_effect"),
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
                "bundle": True,
                "reason": "the sizing knobs move together",
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
    assert variants[1].bundle == "the sizing knobs move together"
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


def test_a_sweep_with_an_unknown_path_fails_before_any_run() -> None:
    """A `set` path naming nothing in the version's config, a typo'd criterion parameter among
    them, or a `policy_set` naming no policy field refuses the whole sweep; a bad value is the
    variant's own refusal, left to its run."""
    config, version = seed_config("stage2-contraction-breakout"), policy_version()
    good = _sweep({"label": "a", "set": ["exits[0].enabled=false"]}, {"label": "b"})
    check_paths(parse_variants(good, RunConventions()), config, version)
    for bad, code in (
        ({"set": ["exits[0].enabeld=false"]}, "backtest_bad_override_path"),
        (
            {"set": ["playbooks[default].setup.criteria[vcp].params.min_dpth_pct=3"]},
            "backtest_bad_override_path",
        ),
        ({"set": ["exits[nope].enabled=false"]}, "backtest_override_no_item"),
        ({"policy_set": ["adv_cap_pctt=0.15"]}, "backtest_bad_override_path"),
        ({"policy_set": ["kill.bot_drawdown_pct=0.15"]}, "backtest_override_no_effect"),
    ):
        sweep = parse_variants(_sweep({"label": "a"}, {"label": "b", **bad}), RunConventions())
        with pytest.raises(BacktestError, match=code):
            check_paths(sweep, config, version)
    for value in ("exits[0].enabled=7", 'exits[+]={"rule": "max_hold", "dayz": 60}'):
        valued = parse_variants(_sweep({"label": "a", "set": [value]}), RunConventions())
        check_paths(valued, config, version)
        with pytest.raises(OverrideError, match="backtest_override_invalid"):
            valued.variants[0].conventions.overrides.apply_config(config)
