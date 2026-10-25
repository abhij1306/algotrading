"""The S1 research rules at their seams: the step trail and the EMA exit (`engine.exits`), the
override selector by type (`backtest.overrides`), the close entry's breakout volume test
(`backtest.fillrules`), the regime without a slope test, the level trigger's entry timing and
the known gaps that name a playbook. Small examples; no run of the loop."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.hat.backtest.fillrules import breakout_volume_ok, first_trigger
from app.hat.backtest.overrides import OverrideError, Overrides
from app.hat.backtest.spec import GAP_PLAYBOOKS
from app.hat.engine import exits
from app.hat.engine.criteria import evaluate_live
from app.hat.engine.criteria.live import LiveContext, PriceBreaksLevelParams
from app.hat.engine.exits import ExitView, PositionState, evaluate
from app.hat.engine.regime_gate import close_above_sma_and_rising
from app.hat.engine.strategy.config import (
    CloseAboveSma,
    ExitCloseBelowEma,
    ExitStepTrail,
    StrategyConfig,
)
from app.hat.engine.strategy.versions import seed_config

D = Decimal
S1 = "stage2-contraction-breakout"
# entry 100, initial stop 90: 1R = 10
POS = PositionState(
    qty_original=10, qty_open=10, avg_entry=D(100), initial_stop=D(90), current_stop=D(90)
)
TRAIL = ExitStepTrail(rule="step_trail", step_r=1.5, move_r=1.0)


def _stop_at(price: str, pos: PositionState = POS) -> Decimal | None:
    return evaluate([TRAIL], pos, ExitView(D(price)), "live").new_stop


# --- step trail ----------------------------------------------------------------------------------


def test_step_trail_moves_one_r_per_step() -> None:
    """The owner's example: -1R -> 0R at +1.5R -> +1R at +3R -> +2R at +4.5R."""
    assert _stop_at("114.9") is None  # below the first step
    assert _stop_at("115") == D(100)
    assert _stop_at("129.9") == D(100)
    assert _stop_at("130") == D(110)
    assert _stop_at("145") == D(120)


def test_step_trail_only_ratchets_up_and_never_fires() -> None:
    raised = PositionState(10, 10, D(100), D(90), current_stop=D(110))
    assert _stop_at("120", raised) is None  # 0R is below the +1R already held
    result = evaluate([TRAIL], raised, ExitView(D(145)), "live")
    assert (result.new_stop, result.fired, result.actions) == (D(120), (), ())
    assert evaluate([TRAIL], POS, ExitView(D(145)), "close").new_stop == D(120)


def test_step_trail_steps_are_found_on_the_way_up() -> None:
    """The day loop's bisection finds each step's first price, one after the other."""
    state = POS
    found = []
    low = D(100)
    while (price := first_trigger([TRAIL], state, low, D(146), D("0.05"))) is not None:
        found.append(price)
        state = exits.apply(state, evaluate([TRAIL], state, ExitView(price), "live"))
        low = price
    assert found == [D(115), D(130), D(145)]
    assert state.current_stop == D(120)


def test_step_trail_move_at_most_step() -> None:
    with pytest.raises(ValidationError, match="move_r_above_step_r"):
        ExitStepTrail(rule="step_trail", step_r=1.0, move_r=1.5)


# --- close below EMA -----------------------------------------------------------------------------


def test_close_below_ema_reads_the_ema_with_the_price_as_close() -> None:
    rule = ExitCloseBelowEma(rule="close_below_ema", ema=3)
    assert exits.history_sessions([rule]) == 17  # 6 x 3 closes with the price
    closes = [D(100)] * 17
    view = exits.view_from_bars(D(96), [rule], 2, closes, closes)
    assert view.ema == {3: D(98)}  # one step of 2/(3+1) from a flat 100
    assert evaluate([rule], POS, view, "close").actions
    flat = exits.view_from_bars(D(100), [rule], 2, closes, closes)
    assert not evaluate([rule], POS, flat, "close").actions
    short = exits.view_from_bars(D(96), [rule], 2, closes[:5], closes[:5])
    assert short.ema == {}  # too little history: no level, no exit


# --- overrides by type ---------------------------------------------------------------------------


def test_select_criteria_and_filters_by_type() -> None:
    changed = Overrides(
        sets=(
            ("playbooks[default].setup.criteria[vcp].params.min_depth_pct", 3),
            ("playbooks[default].entry_filters[max_chase].params.pct", 2),
            ("playbooks[default].setup.criteria[rs_rank#1].enabled", False),
            ("playbooks[default].trigger[volume_pace].enabled", False),
        )
    ).apply_config(seed_config(S1))
    playbook = changed.playbook("default")
    by_type = {c.type: c for c in playbook.setup.criteria}
    assert by_type["vcp"].params["min_depth_pct"] == 3
    assert playbook.entry_filters[0].params["pct"] == 2
    ranks = [c for c in playbook.setup.criteria if c.type == "rs_rank"]
    assert [(c.mode, c.enabled) for c in ranks] == [("hard", True), ("score", False)]


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("playbooks[default].setup.criteria[rs_rank].enabled", "backtest_override_ambiguous"),
        ("playbooks[default].setup.criteria[rs_rank#2].enabled", "backtest_override_no_item"),
        ("playbooks[default].setup.criteria[darvas_box].enabled", "backtest_override_no_item"),
    ],
)
def test_select_by_type_fails_loudly(path: str, code: str) -> None:
    with pytest.raises(OverrideError, match=code):
        Overrides(sets=((path, False),)).apply_config(seed_config(S1))


# --- entry timing and breakout volume ------------------------------------------------------------


def test_breakout_volume_against_the_mean_before() -> None:
    assert breakout_volume_ok([100, 100, 150], 2, 1.5) is True
    assert breakout_volume_ok([100, 100, 149], 2, 1.5) is False
    assert breakout_volume_ok([100, 150], 2, 1.5) is None  # too few sessions
    assert breakout_volume_ok([100, float("nan"), 150], 2, 1.5) is None
    assert breakout_volume_ok([0, 0, 150], 2, 1.5) is None


def test_breakout_volume_only_at_the_close() -> None:
    """An intraday break cannot know the day's volume: the test needs `daily_close`."""
    with pytest.raises(ValidationError, match="breakout_volume_needs_daily_close"):
        PriceBreaksLevelParams(level="pivot", volume_vs_avg=1.5)
    params = PriceBreaksLevelParams(level="pivot", entry_timing="daily_close", volume_vs_avg=1.5)
    assert params.volume_avg_days == 50
    # the defaults stay out of the canonical form, so stored configs keep their hashes
    assert PriceBreaksLevelParams(level="pivot").model_dump(mode="json") == {
        "level": "pivot",
        "bar": "5m",
        "confirm": "close",
    }


def test_daily_close_level_never_breaks_live() -> None:
    ctx = LiveContext(
        at=datetime(2026, 10, 23, 9, 0, tzinfo=UTC),
        levels={"pivot": D(100)},
        bar_high=D(110),
        bar_close=D(108),
    )
    assert evaluate_live("price_breaks_level", {"level": "pivot"}, ctx).passed
    closed = evaluate_live(
        "price_breaks_level", {"level": "pivot", "entry_timing": "daily_close"}, ctx
    )
    assert (closed.passed, closed.reason) == (False, "daily_close_not_live")


def _s1_with(*sets: tuple[str, object]) -> StrategyConfig:
    return Overrides(sets=sets).apply_config(seed_config(S1))


def test_gaps_name_volume_pace_and_daily_close_playbooks() -> None:
    """S1's seed relies on `volume_pace`, which the backtest does not model; a daily-close
    entry is not traded live. Each names the playbook, which blocks its promotion."""
    named = {code for code, test in GAP_PLAYBOOKS.items() if test(seed_config(S1).playbooks[0])}
    assert named == {"volume_pace_not_backtested"}
    plain = _s1_with(("playbooks[default].trigger[volume_pace].enabled", False))
    assert not any(test(plain.playbooks[0]) for test in GAP_PLAYBOOKS.values())
    close = _s1_with(
        ("playbooks[default].trigger[volume_pace].enabled", False),
        ("playbooks[default].trigger[price_breaks_level].params.entry_timing", "daily_close"),
    )
    assert close.playbooks[0].entry_timing == "daily_close"
    assert GAP_PLAYBOOKS["daily_close_entry_not_live"](close.playbooks[0])


# --- regime --------------------------------------------------------------------------------------


def test_regime_rising_days_zero_is_close_above_sma_only() -> None:
    falling = [D(110), D(108), D(106), D(104), D(106)]  # SMA(3) falling, close above it
    flat = CloseAboveSma(rule="close_above_sma_and_rising", sma=3, rising_days=0)
    sloped = CloseAboveSma(rule="close_above_sma_and_rising", sma=3, rising_days=1)
    assert close_above_sma_and_rising(falling, flat)
    assert not close_above_sma_and_rising(falling, sloped)
    assert not close_above_sma_and_rising([*falling[:-1], D(104)], flat)  # close below the SMA
