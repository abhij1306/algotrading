"""The research-variant seams: the new exit rules and the trail start (`engine.exits`), the stop
width cap (`engine.stops`), per-run overrides (`backtest.overrides`, `RunConventions.record`), a
sweep's variants file (`runs.parse_variants`) and the report data's pure parts
(`backtest.report_data`). Small examples; no run of the loop."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.hat.backtest.overrides import OverrideError, Overrides, parse_set, set_path
from app.hat.backtest.report_data import (
    _benchmark_on,
    drawdowns,
    group_stats,
    hold_bucket,
    index_regimes,
    regime_before,
    yearly_returns,
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
    with pytest.raises(OverrideError, match="backtest_override_invalid"):
        Overrides(sets=(("exits[hard_stop].enabled", False),)).apply_config(config)
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
    with pytest.raises(OverrideError):
        Overrides(risk_pct=D(2)).apply_policy(version)


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
    with pytest.raises(BacktestError, match="backtest_promotion_with_overrides"):
        RunConventions(regime_mult="policy", overrides=Overrides(max_positions=3)).check_promotion(
            DEFAULT_SLIPPAGE_BPS
        )


def test_parse_variants_layers_on_the_base() -> None:
    base = RunConventions(overrides=Overrides(sets=(("exits[0].enabled", True),), risk_pct=D(1)))
    variants = parse_variants(
        [
            {"label": "base"},
            {
                "label": "wide",
                "set": ["exits[1].at_r=3"],
                "risk_pct": 0.005,
                "max_positions": 8,
                "sleeve_weight": 0.5,
                "slot_order": "score",
            },
        ],
        base,
    )
    assert variants[0].conventions == base
    wide = variants[1].conventions
    assert wide.overrides == Overrides(
        sets=(("exits[0].enabled", True), ("exits[1].at_r", 3)),
        risk_pct=D("0.005"),
        max_positions=8,
    )
    assert (wide.sleeve_weight, wide.slot_order) == (D("0.5"), "score")
    for bad in ([], [{"label": "a"}, {"label": "a"}], [{"label": "a", "other": 1}], [{"x": 1}]):
        with pytest.raises(BacktestError):
            parse_variants(bad, base)


# --- report data ---------------------------------------------------------------------------------


def test_index_regime_on_the_prior_session() -> None:
    start = date(2020, 1, 1)
    closes = [(start + timedelta(days=n), D(100 + n)) for n in range(230)]
    states = index_regimes(closes)
    assert states[218][1] == "unknown"  # 219 sessions: no SMA 20 sessions back
    assert states[219][1] == "up"
    assert regime_before(states, closes[219][0]) == "unknown"  # the session before counts
    assert regime_before(states, closes[220][0]) == "up"
    assert regime_before(states, start) == "unknown"
    falling = [(d, D(1000 - n)) for n, (d, _) in enumerate(closes)]
    assert index_regimes(falling)[-1][1] == "not_up"


def test_hold_buckets() -> None:
    assert [hold_bucket(n) for n in (0, 5, 6, 20, 21, 60, 61, 400)] == [
        "0-5",
        "0-5",
        "6-20",
        "6-20",
        "21-60",
        "21-60",
        "61+",
        "61+",
    ]


def test_yearly_returns_chain_from_the_initial_equity() -> None:
    equity = [(date(2020, 6, 1), D(110)), (date(2020, 12, 31), D(120)), (date(2021, 3, 1), D(90))]
    found = yearly_returns(equity, D(100))
    assert found[2020] == pytest.approx(0.2)
    assert found[2021] == pytest.approx(-0.25)


def test_group_stats() -> None:
    class T:
        def __init__(self, reason: str, r: str) -> None:
            self.exit_reason, self.r_multiple = reason, D(r)

    trades = [T("b", "2"), T("a", "-1"), T("b", "-1")]
    found = group_stats(trades, lambda t: t.exit_reason, ["b"])  # type: ignore[arg-type]
    assert [(g["key"], g["trades"], g["sum_r"], g["win_rate"]) for g in found] == [
        ("b", 2, "1.000000", 0.5),
        ("a", 1, "-1.000000", 0.0),
    ]


def test_drawdown_counts_from_the_initial_equity() -> None:
    assert drawdowns([90.0, 120.0, 60.0], 100.0) == pytest.approx([-0.1, 0.0, -0.5])
    assert drawdowns([90.0, 99.0]) == pytest.approx([0.0, 0.0])  # no start: the first is the peak


def test_benchmark_is_never_carried_over_a_missing_session() -> None:
    """The benchmark is the index's close on each run session; one missing session gives none."""
    d1, d2, d3 = date(2015, 2, 2), date(2015, 2, 3), date(2015, 2, 4)
    closes = [(d1, D(100)), (d3, D(102))]
    assert _benchmark_on(closes, [d1, d3]) == [D(100), D(102)]
    assert _benchmark_on(closes, [d1, d2, d3]) == []
