"""The S1 research rules at their seams: the step trail and the EMA exit (`engine.exits`), the
override selector by type (`backtest.overrides`), the close entry's breakout volume test
(`backtest.fillrules`), the regime without a slope test, the level trigger's entry timing and
the known gaps that name a playbook, a pivot used once (`engine.scan`), and the S1 book's rules
on one position: breakeven, the half sale, the SMA20 exit's grace window, the chase and stop
width skips, `volume_pace` on the full day, the volume pace convention and the RS slot order.
Small examples; no run of the loop."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import numpy as np
import pytest
from pydantic import ValidationError

from app.hat.backtest.data import last_through
from app.hat.backtest.evidence import RunMetrics, unbacktested
from app.hat.backtest.fillrules import (
    breakout_volume_ok,
    first_trigger,
    full_day_pace_skip,
)
from app.hat.backtest.fills import entry_fill, raised_stop_dip
from app.hat.backtest.overrides import OverrideError, Overrides
from app.hat.backtest.slots import is_slot_order, order_slots
from app.hat.backtest.spec import (
    GAP_PLAYBOOKS,
    VOLUME_PACE_FULL_DAY,
    BacktestError,
    RunConventions,
)
from app.hat.costs import BacktestFills, load_cost_schedule
from app.hat.engine import exits, trigger
from app.hat.engine.criteria import evaluate_live
from app.hat.engine.criteria.base import Bars
from app.hat.engine.criteria.live import LiveContext, PriceBreaksLevelParams
from app.hat.engine.exits import ExitView, PositionState, evaluate
from app.hat.engine.regime_gate import close_above_sma_and_rising
from app.hat.engine.scan import pivot_date, setup_taken
from app.hat.engine.stops import SKIP_TOO_WIDE, final_contraction_low
from app.hat.engine.strategy.config import (
    CloseAboveSma,
    ExitBreakeven,
    ExitCloseBelowEma,
    ExitCloseBelowSma,
    ExitHardStop,
    ExitPartialAtR,
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
    overrides, config = Overrides(sets=((path, False),)), seed_config(S1)
    with pytest.raises(OverrideError, match=code):
        overrides.apply_config(config)


# --- entry timing and breakout volume ------------------------------------------------------------


def test_breakout_volume_against_the_mean_before() -> None:
    assert breakout_volume_ok([100, 100, 150], 2, 1.5) is True
    assert breakout_volume_ok([100, 100, 149], 2, 1.5) is False
    assert breakout_volume_ok([100, 150], 2, 1.5) is None  # too few sessions
    assert breakout_volume_ok([100, float("nan"), 150], 2, 1.5) is None
    assert breakout_volume_ok([0, 0, 150], 2, 1.5) is None


def test_volume_window_counts_every_stored_session() -> None:
    """The breakout average reads the last sessions on or before the day from the volume series
    of every stored session (a session with bad prices included, its volume NaN when missing), so
    a dropped price row never pulls an older session into the window."""
    days = np.array([1, 2, 3, 5, 6])
    volumes = np.array([10.0, 20.0, 30.0, 50.0, 60.0])
    assert last_through(days, volumes, 5, 3) == [20.0, 30.0, 50.0]
    assert last_through(days, volumes, 4, 2) == [20.0, 30.0]  # day 4 has no session
    assert last_through(days, volumes, 0, 3) == []
    volumes[2] = float("nan")  # a session whose volume the store lacks
    assert breakout_volume_ok(last_through(days, volumes, 5, 3), 2, 1.0) is None  # fails closed


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
    assert named == {"trigger_is_level_break_only", "volume_pace_on_full_day_volume"}
    plain = _s1_with(("playbooks[default].trigger[volume_pace].enabled", False))
    assert not any(test(plain.playbooks[0]) for test in GAP_PLAYBOOKS.values())
    close = _s1_with(
        ("playbooks[default].trigger[volume_pace].enabled", False),
        ("playbooks[default].trigger[price_breaks_level].params.entry_timing", "daily_close"),
    )
    assert close.playbooks[0].entry_timing == "daily_close"
    assert GAP_PLAYBOOKS["daily_close_entry_not_live"](close.playbooks[0])


def test_runs_recorded_before_the_volume_pace_gap_still_name_it() -> None:
    """A run made before this gap named a playbook recorded `trigger_is_level_break_only` like
    every run, so it already names S1's `volume_pace` playbook for the comparison and the gate."""
    earlier = RunMetrics.from_json({"known_gaps": ["trigger_is_level_break_only"]})
    assert unbacktested(earlier, seed_config(S1).playbooks) == frozenset({"default"})


# --- regime --------------------------------------------------------------------------------------


def test_regime_rising_days_zero_is_close_above_sma_only() -> None:
    falling = [D(110), D(108), D(106), D(104), D(106)]  # SMA(3) falling, close above it
    flat = CloseAboveSma(rule="close_above_sma_and_rising", sma=3, rising_days=0)
    sloped = CloseAboveSma(rule="close_above_sma_and_rising", sma=3, rising_days=1)
    assert close_above_sma_and_rising(falling, flat)
    assert not close_above_sma_and_rising(falling, sloped)
    assert not close_above_sma_and_rising([*falling[:-1], D(104)], flat)  # close below the SMA


def _highs(*highs: float | None) -> Bars:
    days = [date(2021, 3, d) for d in range(1, len(highs) + 1)]
    empty = [None] * len(highs)
    return Bars(days, list(highs), empty, empty, empty, empty, empty)


def test_pivot_date_is_the_latest_bar_with_that_high() -> None:
    bars = _highs(101.5, 104.25, None, 99.0, 104.25, 98.0)
    assert pivot_date(bars, D("104.25")) == date(2021, 3, 5)
    assert pivot_date(bars, D("101.5")) == date(2021, 3, 1)


def test_pivot_date_without_a_bar_at_the_pivot() -> None:
    bars = _highs(101.5, 104.25)
    assert pivot_date(bars, D("104.2")) is None  # a level no high made, e.g. a close
    assert pivot_date(bars, None) is None


def test_held_instrument_is_never_republished() -> None:
    pivot_on = date(2021, 3, 5)
    assert setup_taken("A", pivot_on, {"A"}, ())
    assert setup_taken("A", None, {"A"}, ())
    assert not setup_taken("B", pivot_on, {"A"}, ())


def test_a_used_pivot_needs_a_new_one() -> None:
    used = {("A", date(2021, 3, 5))}
    assert setup_taken("A", date(2021, 3, 5), (), used)  # same setup after the exit
    assert not setup_taken("A", date(2021, 4, 9), (), used)  # a new final contraction
    assert not setup_taken("B", date(2021, 3, 5), (), used)  # another name's pivot
    assert not setup_taken("A", None, (), used)  # no pivot date: the held rule only


# --- the S1 book's entry and exit rules on one position ------------------------------------------

# Breakeven at +2.5R, 50% sold at +3R, the rest under SMA20 after a 10-session grace window.
BOOK = [
    ExitHardStop(rule="hard_stop"),
    ExitBreakeven(rule="move_stop_to_breakeven", at_r=2.5),
    ExitPartialAtR(rule="partial_at_r", at_r=3, sell_fraction=0.5),
    ExitCloseBelowSma(rule="close_below_sma", sma=20, grace_sessions=10),
]


def _close_view(price: str, days_held: int) -> ExitView:
    return ExitView(D(price), days_held=days_held, sma={20: D(110)})


def test_breakeven_moves_the_stop_at_two_and_a_half_r() -> None:
    assert evaluate(BOOK, POS, ExitView(D("124.95")), "live").new_stop is None
    assert evaluate(BOOK, POS, ExitView(D(125)), "live").new_stop == D(100)


def test_half_the_position_sells_at_three_r() -> None:
    raised = PositionState(10, 10, D(100), D(90), current_stop=D(100), fired=frozenset({"1"}))
    assert not evaluate(BOOK, raised, ExitView(D("129.95")), "live").actions
    (sale,) = evaluate(BOOK, raised, ExitView(D(130)), "live").actions
    assert (sale.kind, sale.qty) == ("partial", 5)


def test_sma20_exit_waits_out_the_grace_window() -> None:
    """No SMA exit in the 10 sessions after the entry session; the stop still acts in them."""
    assert not evaluate(BOOK, POS, _close_view("109", 10), "close").actions
    (sale,) = evaluate(BOOK, POS, _close_view("109", 11), "close").actions
    assert (sale.kind, sale.qty) == ("full", 10)
    assert not evaluate(BOOK, POS, _close_view("111", 11), "close").actions  # above the SMA
    (stopped,) = evaluate(BOOK, POS, ExitView(D(90), days_held=2), "live").actions
    assert stopped.kind == "full"
    plain = ExitCloseBelowSma(rule="close_below_sma", sma=20)
    assert evaluate([plain], POS, _close_view("109", 0), "close").actions  # off by default
    assert "grace_sessions" not in plain.model_dump(mode="json")  # the stored hashes keep


def test_book_exits_act_together_on_one_position() -> None:
    """One day's way up from 100 to 131 raises the stop to breakeven at 125 then sells half at
    130; a low back at the raised stop with a close above it is the ambiguous day the
    `pessimistic` convention fills; the rest leaves on a close under SMA20 after the grace."""
    state, found, low = POS, [], D(100)
    while (price := first_trigger(BOOK, state, low, D(131), D("0.05"))) is not None:
        found.append(price)
        state = exits.apply(state, evaluate(BOOK, state, ExitView(price), "live"))
        low = price
    assert found == [D(125), D(130)]
    assert (state.current_stop, state.qty_open) == (D(100), 5)
    assert raised_stop_dip(D(100), D(128), D(90), state.current_stop)
    assert not raised_stop_dip(D("100.05"), D(128), D(90), state.current_stop)
    assert not evaluate(BOOK, state, _close_view("109", 7), "close").actions
    (rest,) = evaluate(BOOK, state, _close_view("109", 12), "close").actions
    assert (rest.kind, rest.qty) == ("full", 5)


def test_max_chase_skips_a_gap_over_two_pct() -> None:
    """A gap-up open fills at the open: within 2% of the pivot it passes, beyond it fails."""
    at = datetime(2021, 3, 1, 4, 0, tzinfo=UTC)
    for open_, passed in (("102", True), ("102.05", False)):
        price = entry_fill(D(open_), D(105), D(100), D("0.05"))
        assert price == D(open_)
        ctx = LiveContext(at=at, price=price, trigger_level=D(100), locked_upper_circuit=False)
        assert evaluate_live("max_chase", {"pct": 2}, ctx).passed is passed


def test_stop_skip_beyond_ten_pct() -> None:
    """0.5% under the final contraction low, skipped when more than 10% under the entry."""
    kept = final_contraction_low(D(100), D(91), D("0.5"), D(10), D("0.05"))
    assert (kept.stop, kept.skipped) == (D("90.50"), False)
    wide = final_contraction_low(D(100), D("90.40"), D("0.5"), D(10), D("0.05"))
    assert (wide.stop, wide.skip) == (None, SKIP_TOO_WIDE)


def test_volume_pace_on_the_full_day_volume() -> None:
    """The daily estimate's `volume_pace`: the day's volume against vs_avg50 x the mean of the
    50 sessions before it."""
    assert trigger.volume_pace_multiple(seed_config(S1).playbooks[0]) == 1.5
    plain = _s1_with(("playbooks[default].trigger[volume_pace].enabled", False))
    assert trigger.volume_pace_multiple(plain.playbooks[0]) is None
    stricter = _s1_with(
        ("playbooks[default].trigger[+]", {"type": "volume_pace", "params": {"vs_avg50": 2}})
    )
    assert trigger.volume_pace_multiple(stricter.playbooks[0]) == 2  # each one blocks live
    before = [100.0] * 50
    assert full_day_pace_skip([*before, 150.0], 1.5) is None
    assert full_day_pace_skip([*before, 149.0], 1.5) == "volume_pace_low"
    assert full_day_pace_skip(before, 1.5) == "volume_pace_missing"  # 49 sessions before it
    assert full_day_pace_skip([*before[:-1], float("nan"), 150.0], 1.5) == "volume_pace_missing"


def test_volume_pace_convention() -> None:
    """`full_day` is recorded in the conventions (a default run keeps its digest); an unknown
    rule is refused; the RS slot order may evidence a version."""
    fills = BacktestFills(load_cost_schedule(), D(20))
    window = (date(2021, 1, 1), date(2021, 12, 31))
    default = RunConventions().record(fills, window, D("0.5"), {})
    assert "volume_pace" not in default
    forced = RunConventions(volume_pace=VOLUME_PACE_FULL_DAY).record(fills, window, D("0.5"), {})
    assert forced["volume_pace"] == "full_day"
    with pytest.raises(BacktestError, match="backtest_bad_volume_pace"):
        RunConventions(volume_pace="pace")
    RunConventions(slot_order="rs", regime_mult="policy").check_promotion(D(20))
    with pytest.raises(BacktestError, match="backtest_promotion_off_time_order"):
        RunConventions(slot_order="score", regime_mult="policy").check_promotion(D(20))


def test_rs_slot_order_takes_the_strongest_first() -> None:
    """Ties and missing scores keep the watchlist's score order, those without one last."""
    rs = {"a": D(5), "b": None, "c": D(9), "d": D(5)}
    ordered = order_slots(list(rs), "rs", date(2021, 3, 1), lambda _: (0, D(0)), rs.get)
    assert ordered == ["c", "a", "d", "b"]
    assert is_slot_order("rs")
