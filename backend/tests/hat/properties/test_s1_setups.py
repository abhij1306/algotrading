"""The S1 setups 2-5, their shared rules and tags, the trigger validity window and the one row
per instrument, at their seams: one passing and one failing small series per rule, fed to the
pure function; the scan identity at `scan_bot` on two synthetic instruments. No run of the loop,
the pipeline or a database."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import polars as pl
import pytest
from setups_series import (
    CHEAT,
    HANDLE,
    POWER,
    PRIMARY,
    ctx,
    days,
    frame,
    path,
    quiet,
)

from app.hat.backtest.data import MarketData, MarketView
from app.hat.backtest.fillrules import SKIP_PACE_LOW, SKIP_PACE_MISSING
from app.hat.backtest.loop import (
    SKIP_LIMIT_NOT_REACHED,
    SKIP_LOW_VOLUME,
    SKIP_NO_VOLUME,
    consumes,
)
from app.hat.backtest.slots import is_slot_order, order_slots
from app.hat.engine import scan
from app.hat.engine.criteria import REGISTRY, evaluate
from app.hat.engine.criteria import setups as s
from app.hat.engine.criteria.base import RULE_STOP_BUFFER, EvalContext
from app.hat.engine.criteria.book import Contraction, series_of
from app.hat.engine.stops import SKIP_BAD_INPUT
from app.hat.engine.strategy.config import StrategyConfig, parse_config
from app.hat.engine.strategy.scoring import playbook_context, stop_buffer_pct

D = Decimal


def _run(closes: list[float], quiet_n: int, **levels: float) -> Any:  # noqa: ANN401
    return ctx(frame(closes, quiet(quiet_n, len(closes))), levels=levels or None)


# --- shared: vr, dry-up, stop cap -----------------------------------------------------------


def test_volume_ratio_reads_the_50_sessions_before() -> None:
    volume: list[float | None] = [100.0] * 50 + [150.0]
    assert s.volume_ratio(volume, 50) == D("1.5")
    assert s.volume_ratio(volume, 49) is None  # 49 sessions before it
    assert s.volume_ratio([*volume[:-2], None, 150.0], 50) is None


def test_dryup_needs_a_low_mean_and_one_quiet_session() -> None:
    params = s.BookCheatParams()
    assert s.dryup_fault((D("0.6"), D("0.4")), params) is None
    assert s.dryup_fault((D("0.7"), D("0.4")), params) == "no_volume_dryup"
    assert s.dryup_fault((D("0.6"), D("0.5")), params) == "no_dryup_session"
    assert s.dryup_fault(None, params) == "no_volume_data"
    off = s.BookCheatParams(dryup_mean_max=None, dryup_session_max=None)
    assert s.dryup_fault(None, off) is None


def test_area_dryup_over_the_tight_area() -> None:
    volume: list[float | None] = [100.0] * 50 + [60.0, 40.0]
    second = D(40) / D("99.2")  # ADV50 of the 51st bar holds the 60 before it
    assert s.area_dryup(volume, Contraction(50, 51, D(1), D(1))) == (
        (D("0.6") + second) / 2,
        second,
    )


def _capped(low: float, buffer_pct: float = 0.5) -> Any:  # noqa: ANN401
    staged = _run(CHEAT, 15, pivot=100.0, final_contraction_low=low)
    return evaluate("stop_cap", {}, staged.with_rules({RULE_STOP_BUFFER: buffer_pct}))


def test_stop_cap() -> None:
    passed = _capped(91.0)
    assert passed.passed
    assert passed.value == pytest.approx(9.455)
    wide = _capped(90.4)
    assert (wide.passed, wide.reason) == (False, "stop_too_wide")
    assert _capped(91.0, buffer_pct=1.0).value == pytest.approx(9.91)  # the playbook's buffer
    assert evaluate("stop_cap", {}, _run(CHEAT, 15)).reason == "no_data"
    outside = _run(CHEAT, 15, pivot=100.0, final_contraction_low=91.0)  # no playbook: no buffer
    assert evaluate("stop_cap", {}, outside).reason == "no_data"


def test_playbook_context_reads_the_playbooks_stop_buffer() -> None:
    playbook = S1_SETUPS["playbooks"][0]
    own = {**playbook, "stop": {**playbook["stop"], "buffer_pct": 1}}
    config = parse_config({**S1_SETUPS, "playbooks": [own]})
    assert playbook_context(config.playbooks[0], _run(CHEAT, 15)).rules == {RULE_STOP_BUFFER: 1}
    other = {**playbook, "stop": {"rule": "pct_below_entry", "pct": 3}}
    plain = parse_config({**S1_SETUPS, "playbooks": [other]}).playbooks[0]
    assert playbook_context(plain, _run(CHEAT, 15)).rules == {RULE_STOP_BUFFER: 0.5}
    assert stop_buffer_pct(plain) == 0.5  # the contraction stop's default


def test_data_needs_name_the_features() -> None:
    assert "features_daily" in REGISTRY["setup_tags"].data_needs
    assert "features_daily" in REGISTRY["book_primary_base"].data_needs


# --- Setup 2: the cup and the cheat ---------------------------------------------------------


def _series(closes: list[float]) -> tuple[list[float], list[float]]:
    data = frame(closes, [1.0] * len(closes))
    return data["high"].to_list(), data["low"].to_list()


@pytest.mark.parametrize(
    ("closes", "reason"),
    [
        (CHEAT, None),
        (path([(300, 100.0), (30, 90.0), (30, 92.0)], 40.0), "no_cup"),  # 10% deep
        (path([(300, 100.0), (30, 45.0), (30, 50.0)], 40.0), "cup_too_deep"),
        (path([(300, 100.0), (15, 75.0), (10, 78.0)], 40.0), "cup_too_short"),
    ],
)
def test_cup_rules(closes: list[float], reason: str | None) -> None:
    high, low = _series(closes)
    found = s.cup_of(high, low, len(closes) - 1, s.BookCheatParams())
    assert (found[0] if isinstance(found, tuple) else None) == reason


def test_cup_too_long() -> None:
    high, low = _series(CHEAT)
    found = s.cup_of(high, low, len(CHEAT) - 1, s.BookCheatParams(cup_sessions=[35, 50]))
    assert isinstance(found, tuple)
    assert found[0] == "cup_too_long"


def test_pause_is_the_longest_tight_run() -> None:
    high, low = _series(CHEAT)
    pause = s.pause_of(high, low, len(CHEAT) - 1, s.BookCheatParams())
    assert pause is not None
    assert (pause.sessions, pause.depth_pct <= 6) == (15, True)
    rising = path([(300, 100.0), (30, 70.0), (10, 95.0)], 40.0)
    high, low = _series(rising)
    assert s.pause_of(high, low, len(rising) - 1, s.BookCheatParams()) is None


def test_cheat_position_names_the_setup() -> None:
    params = s.BookCheatParams()
    assert s.cheat_kind(D("0.5"), params) == s.CHEAT_3C
    assert s.cheat_kind(D("0.33"), params) == s.CHEAT_3C
    assert s.cheat_kind(D("0.2"), params) == s.LOW_CHEAT
    assert s.cheat_kind(D("0.68"), params) is None


def test_book_cheat_passes_a_3c_pause() -> None:
    found = s.book_cheat(_run(CHEAT, 15), s.BookCheatParams())
    assert (found.passed, found.extras["setup"]) == (True, s.CHEAT_3C)
    assert found.extras["pivot"] == pytest.approx(82.41)
    assert found.extras["footprint"].startswith("12W 31/4")
    assert found.extras["mine"]["pause_range_max_pct"] == 6.0  # the [mine] values, logged


@pytest.mark.parametrize(
    ("closes", "params", "reason"),
    [
        (CHEAT, {"low_before_pause_sessions": 25}, "low_too_recent"),
        (
            path([(300, 100.0), (30, 70.0), (20, 92.0), (10, 92.0)], 40.0),
            {},
            "pause_in_upper_third",
        ),
        # no uptrend into H0: the template fails on its bar
        (
            [100.0] * 300 + path([(30, 70.0), (20, 82.0), (10, 82.0)], 100.0),
            {},
            "h0_not_in_uptrend",
        ),
    ],
)
def test_book_cheat_rules(closes: list[float], params: dict[str, Any], reason: str) -> None:
    found = s.book_cheat(_run(closes, 15), s.BookCheatParams(**params))
    assert (found.passed, found.reason) == (False, reason)


def test_book_cheat_needs_the_pause_to_dry_up() -> None:
    found = s.book_cheat(_run(CHEAT, 0), s.BookCheatParams())
    assert (found.passed, found.reason) == (False, "no_volume_dryup")


# --- Setup 3: cup with handle ---------------------------------------------------------------


def test_book_cup_handle_passes() -> None:
    found = s.book_cup_handle(_run(HANDLE, 11), s.BookCupHandleParams())
    assert (found.passed, found.extras["setup"]) == (True, s.CUP_HANDLE)
    assert found.extras["pivot"] == pytest.approx(95.475)


@pytest.mark.parametrize(
    ("closes", "params", "reason"),
    [
        (HANDLE, {"handle_sessions": [13, 25]}, "handle_too_short"),
        (HANDLE, {"handle_sessions": [5, 8]}, "handle_too_long"),
        (path([(300, 100.0), (30, 70.0), (25, 95.0), (11, 87.0)], 40.0), {}, "handle_too_low"),
        (HANDLE, {"handle_depth_max_pct": 4.0}, "handle_too_deep"),
    ],
)
def test_handle_rules(closes: list[float], params: dict[str, Any], reason: str) -> None:
    found = s.book_cup_handle(_run(closes, 11), s.BookCupHandleParams(**params))
    assert (found.passed, found.reason) == (False, reason)


def test_handle_at_most_h0() -> None:
    high, low = _series(HANDLE)
    params = s.BookCupHandleParams()
    cup = s.cup_of(high, low, len(HANDLE) - 1, params)
    assert isinstance(cup, s.Cup)
    assert isinstance(s.handle_of(high, low, cup, len(HANDLE) - 1, params), Contraction)
    lower = replace(cup, top=D(95))  # a lip above H0 cannot be in the cup's window: a seam test
    found = s.handle_of(high, low, lower, len(HANDLE) - 1, params)
    assert isinstance(found, tuple)
    assert found[0] == "handle_above_h0"


def test_book_cup_handle_needs_the_handle_to_dry_up() -> None:
    found = s.book_cup_handle(_run(HANDLE, 0), s.BookCupHandleParams())
    assert found.reason == "no_volume_dryup"


# --- Setup 4: power play --------------------------------------------------------------------


def test_book_power_play_passes() -> None:
    found = s.book_power_play(_run(POWER, 8), s.BookPowerPlayParams())
    assert (found.passed, found.extras["setup"], found.extras["contraction_count"]) == (
        True,
        s.POWER_PLAY,
        3,
    )
    assert found.extras["thrust_multiple"] == pytest.approx(2.222, abs=1e-3)


@pytest.mark.parametrize(
    ("closes", "params", "reason"),
    [
        (POWER, {"thrust_multiple": 2.5}, "no_thrust"),
        (POWER, {"flag_sessions": [25, 30]}, "base_too_short"),
        (POWER, {"flag_depth_max_pct": 10.0}, "flag_too_deep"),
        (POWER, {"min_contractions": 4}, "too_few_contractions"),
        (POWER, {"final_depth_max_pct": 1.0}, "no_tight_window"),
        (POWER, {"depth_ratio_max": 0.05}, "no_tight_window"),
    ],
)
def test_power_play_rules(closes: list[float], params: dict[str, Any], reason: str) -> None:
    found = s.book_power_play(_run(closes, 8), s.BookPowerPlayParams(**params))
    assert (found.passed, found.reason) == (False, reason)


def test_book_power_play_needs_the_final_contraction_to_dry_up() -> None:
    found = s.book_power_play(_run(POWER, 0), s.BookPowerPlayParams())
    assert found.reason == "no_volume_dryup"


# --- Setup 5: primary base ------------------------------------------------------------------


def _universe(rs: float) -> pl.DataFrame:
    return pl.DataFrame({"instrument_id": ["i"], "rs_eligible": [rs]})


def test_book_primary_base_passes() -> None:
    found = s.book_primary_base(_run(PRIMARY, 10), s.BookPrimaryBaseParams())
    assert (found.passed, found.extras["setup"], found.extras["history_sessions"]) == (
        True,
        s.PRIMARY_BASE,
        127,
    )


def _gapped(first: int, gap: int, calendar: list[date]) -> pl.DataFrame:
    """PRIMARY's closes on `calendar` from session `first`, missing `gap` sessions after the
    first 30 of them (a suspension)."""
    sessions = calendar[first : first + 30] + calendar[first + 30 + gap :]
    closes = ([50.0] * len(calendar) + PRIMARY)[-len(sessions) :]
    return frame(closes, quiet(10, len(sessions))).with_columns(pl.Series("date", sessions))


def test_listing_age_counts_the_calendars_sessions() -> None:
    calendar = [date(2015, 1, 1) + timedelta(days=n) for n in range(700)]
    # listed in 2010: its first bar in the window is the window's first session
    old = _gapped(0, 250, calendar)
    staged = ctx(old, calendar=calendar)
    assert old.height == 450  # 450 bars would read as a recent listing
    assert s.listed_sessions(staged) is None
    found = s.book_primary_base(staged, s.BookPrimaryBaseParams())
    assert (found.passed, found.reason) == (False, "not_a_recent_listing")
    # listed inside the window, then suspended 250 sessions: 600 sessions old, not 350 bars
    young = ctx(_gapped(100, 250, calendar), calendar=calendar)
    assert s.listed_sessions(young) == 600
    assert s.listed_sessions(ctx(old, calendar=[])) is None  # no calendar: fails closed


def test_a_skip_after_the_break_ends_the_window() -> None:
    """The backtest keeps the setup of a row skipped after its level broke (`loop.consumes`),
    as live's signal of the skipped trigger closes it; a volume or pace skip is no trigger."""
    for reason in (SKIP_LIMIT_NOT_REACHED, SKIP_BAD_INPUT, "stop_too_wide", "max_chase"):
        assert consumes(reason)
    for reason in (SKIP_LOW_VOLUME, SKIP_NO_VOLUME, SKIP_PACE_LOW, SKIP_PACE_MISSING):
        assert not consumes(reason)
    first = _first(BARS)
    key = scan.window_key(first.playbook, first.instrument_id, first.criteria_results)
    assert key == ("cheat", "a", date.fromisoformat(first.criteria_results["pivot_date"]))
    assert scan.window_key("cheat", "a", {}) is None
    window = _found(first, BARS["date"].max() - timedelta(1))
    result = scan.scan_bot(CONFIG, _inputs(BARS, windows=[window], triggered={key}))
    assert (_a(result), _windows(result)["cheat:a"]) == ([], scan.TRIGGERED)


@pytest.mark.parametrize(
    ("closes", "params", "reason"),
    [
        ([50.0] * 600 + PRIMARY, {}, "not_a_recent_listing"),
        (PRIMARY, {"base_depth_max_pct": 15.0}, "base_too_deep"),
        (PRIMARY, {"base_min_sessions": 40}, "base_too_short"),
        (PRIMARY, {"contractions": [4, 6]}, "too_few_contractions"),
        (PRIMARY, {"final_depth_max_pct": 1.0}, "no_tight_window"),
        (PRIMARY, {"depth_ratio_max": 0.05}, "no_tight_window"),
    ],
)
def test_primary_base_rules(closes: list[float], params: dict[str, Any], reason: str) -> None:
    found = s.book_primary_base(_run(closes, 10), s.BookPrimaryBaseParams(**params))
    assert (found.passed, found.reason) == (False, reason)


def test_primary_base_gates_by_history() -> None:
    """Close above the SMA50 from 50 sessions of history, RS >= 70 from 252."""
    params = s.BookPrimaryBaseParams()
    rising = frame(path([(300, 100.0)], 40.0), [1.0] * 300)
    falling = frame(path([(300, 40.0)], 100.0), [1.0] * 300)
    assert s.primary_gate_fault(ctx(rising, universe=_universe(80.0)), 300, params) is None
    below = s.primary_gate_fault(ctx(falling, universe=_universe(80.0)), 300, params)
    assert below is not None
    assert below[0] == "below_sma50"
    assert s.primary_gate_fault(ctx(falling), 49, params) is None  # too short for the SMA50
    weak = s.primary_gate_fault(ctx(rising, universe=_universe(60.0)), 252, params)
    assert weak == ("rs_below_min", 60.0)
    assert s.primary_gate_fault(ctx(rising, universe=_universe(60.0)), 251, params) is None


def test_primary_base_needs_the_final_contraction_to_dry_up() -> None:
    assert s.book_primary_base(_run(PRIMARY, 0), s.BookPrimaryBaseParams()).reason == (
        "no_volume_dryup"
    )


def test_every_failure_reason_has_a_gate() -> None:
    assert s.gate_of("book_cheat", "low_too_recent") == "pause"
    assert s.gate_of("book_power_play", "flag_too_deep") == "flag"
    assert s.gate_of("book_primary_base", "rs_below_min") == "primary_gates"
    assert s.gate_of("book_cup_handle", "no_volume_dryup") == "dry_up"


# --- B: overhead supply ---------------------------------------------------------------------


def _supply(old: float, fall_to: float) -> tuple[list[float], list[float]]:
    """400 bars: an old high `old` 400 sessions back, a fall to `fall_to`, then 100."""
    high = [50.0] * 20 + [old] + [fall_to] * 30 + [100.0] * 349
    return high, [h * 0.99 for h in high]


@pytest.mark.parametrize(
    ("old", "fall_to", "found"),
    [(105.0, 70.0, True), (111.0, 70.0, False), (99.0, 60.0, False), (105.0, 80.0, False)],
)
def test_overhead_supply(old: float, fall_to: float, found: bool) -> None:
    high, low = _supply(old, fall_to)
    supply = s.overhead_supply_of(high, low, D(100), s.OverheadSupplyParams())
    assert (supply is not None) is found
    if supply is not None:
        assert (supply.index, supply.above_pct) == (20, D(5))


def test_overhead_supply_reads_only_326_sessions_back_and_older() -> None:
    high, low = _supply(105.0, 70.0)
    recent = s.OverheadSupplyParams(sessions_ago=[400, 750])  # the old high is 379 back
    assert s.overhead_supply_of(high, low, D(100), recent) is None


# --- tags A, C, D, E ------------------------------------------------------------------------


def _shakeout(undercut_to: float) -> tuple[Any, int]:
    closes = path([(60, 100.0), (5, 90.0), (5, 97.0), (4, undercut_to), (2, 92.0), (3, 95.0)], 60.0)
    volume = [1000.0] * len(closes)
    low_bar = 60 + 5 + 5 + 4 - 1
    volume[low_bar + 1] = volume[low_bar + 2] = 2000.0
    series = series_of(ctx(frame(closes, volume)))
    assert series is not None
    return series, 59


def test_shakeout_tag() -> None:
    params = s.SetupTagsParams()
    series, start = _shakeout(88.0)
    undercut = s.shakeout_of(series, s.thresholds(series), start, params)
    assert undercut == pytest.approx(D("2.222"), abs=D("0.001"))
    deep, start = _shakeout(84.0)  # 6.7% under the earlier low
    assert s.shakeout_of(deep, s.thresholds(deep), start, params) is None
    quiet_back = s.SetupTagsParams(shakeout_vr_min=2.5)
    assert s.shakeout_of(series, s.thresholds(series), start, quiet_back) is None


def test_ath_pivot_tag() -> None:
    high = [90.0, 100.0, 95.0, 80.0, 99.0]
    assert s.ath_pivot(high, 3, D(100)) is True
    assert s.ath_pivot(high, 3, D(99)) is False
    assert s.ath_pivot(high, 0, D(100)) is None


def test_early_leader_tag() -> None:
    params = s.SetupTagsParams(leader_sessions=20)
    stamp = days(20)
    falling = [(d, 100.0 - i) for i, d in enumerate(stamp)]  # ends 19% under its high
    rising = [10.0 + i for i in range(20)]
    assert s.early_leader(stamp, rising, falling, params) is True
    assert s.early_leader(stamp, [*rising[:-1], 10.0], falling, params) is False
    flat = [(d, 100.0) for d in stamp]
    assert s.early_leader(stamp, rising, flat, params) is False
    assert s.early_leader(stamp[:10], rising[:10], falling[:10], params) is None


@pytest.mark.parametrize(
    ("closes", "rs", "stage"),
    [
        (path([(300, 100.0)], 40.0), 80.0, s.STAGE_2),
        (path([(300, 100.0)], 40.0), 50.0, s.STAGE_3),  # RS too weak today, template held before
        (path([(300, 40.0)], 100.0), 80.0, s.STAGE_4),
        ([100.0 + (i % 2) for i in range(300)], 80.0, s.STAGE_1),
        (path([(150, 100.0), (150, 90.0)], 40.0), 80.0, s.UNCLASSIFIED),
    ],
)
def test_stage_tag(closes: list[float], rs: float, stage: str) -> None:
    high, low = _series(closes)
    assert s.stage_of(high, low, closes, rs, s.SetupTagsParams()) == stage


def test_setup_tags_never_fails() -> None:
    found = evaluate("setup_tags", {}, _run(CHEAT, 15, pivot=82.41, base_length_sessions=61.0))
    assert found.passed
    assert set(found.extras) >= {"stage", "early_leader", "shakeout", "ath_pivot"}
    data = frame(CHEAT, quiet(15, len(CHEAT)))
    no_bar = EvalContext(as_of=data["date"][-1] + timedelta(1), instrument_id="i", bars=data)
    assert evaluate("setup_tags", {}, no_bar).passed


# --- the trigger validity window ------------------------------------------------------------


def test_window_session_counts_the_sessions_since_found() -> None:
    stamp = days(10)
    assert scan.window_session(stamp, stamp[-1]) == 1  # found tonight: the next session is D+1
    assert scan.window_session(stamp, stamp[-5]) == 5


def test_a_swing_high_above_the_pivot_cancels() -> None:
    closes = [100.0] * 60 + [106.0, 108.0, 104.0, 100.0]
    series = series_of(ctx(frame(closes, [1.0] * len(closes))))
    assert series is not None
    assert s.swing_high_above(series, 60, D(105)) is True
    assert s.swing_high_above(series, 60, D(110)) is False  # the high stayed under it
    assert s.swing_high_above(series, 60, D(105)) is not s.swing_high_above(series, 63, D(105))


S1_SETUPS: dict[str, Any] = {
    "bot": "s1-setups",
    "universe": [{"type": "series_in", "params": {"series": ["EQ"]}}],
    "regime": {
        "index": "NIFTY 500",
        "rule": "close_above_sma_and_rising",
        "params": {"sma": 50, "rising_days": 0},
    },
    "one_setup_per_instrument": True,
    "playbooks": [
        {
            "name": name,
            "valid_sessions": 5,
            "setup": {
                "criteria": [
                    {"type": code, "mode": "hard", "order": 1, "params": {}},
                    {"type": "stop_cap", "mode": "hard", "order": 2, "params": {}},
                    {"type": "overhead_supply", "mode": "hard", "order": 3, "params": {}},
                    {"type": "setup_tags", "mode": "hard", "order": 4, "params": {}},
                ]
            },
            "trigger": [{"type": "price_breaks_level", "params": {"level": "pivot"}}],
            "stop": {"rule": "below_final_contraction_low", "buffer_pct": 0.5, "max_pct": 10},
        }
        for name, code in (
            ("cheat", "book_cheat"),
            ("handle", "book_cup_handle"),
            ("power", "book_power_play"),
            ("primary", "book_primary_base"),
        )
    ],
    "exits": [{"rule": "hard_stop"}],
}


def _bars(series: dict[str, list[float]], quiet_n: dict[str, int]) -> pl.DataFrame:
    parts = []
    n = max(len(c) for c in series.values())
    for instrument_id, closes in series.items():
        data = frame(closes, quiet(quiet_n[instrument_id], len(closes)))
        stamp = days(n)[n - len(closes) :]
        parts.append(
            data.with_columns(
                pl.Series("date", stamp), pl.lit(instrument_id).alias("instrument_id")
            )
        )
    return pl.concat(parts)


def _inputs(bars: pl.DataFrame, **changes: Any) -> scan.ScanInputs:  # noqa: ANN401
    as_of = bars["date"].max()
    base = scan.ScanInputs(
        as_of=as_of,
        members=sorted(set(bars["instrument_id"].to_list())),
        bars=bars,
        regime_closes=[D(n) for n in range(1, 61)],  # a rising index: the gate is on
        series={"a": "EQ", "b": "EQ", "c": "EQ"},
        sessions_ahead=[as_of + timedelta(days=i) for i in range(1, 21)],
    )
    return replace(base, **changes)


CONFIG = parse_config(S1_SETUPS)
BARS = _bars({"a": CHEAT, "b": HANDLE, "c": [50.0] * len(CHEAT)}, {"a": 15, "b": 11, "c": 0})


def test_scan_rows_carry_the_setup_fields() -> None:
    rows = {r.instrument_id: r for r in scan.scan_bot(CONFIG, _inputs(BARS)).rows}
    assert set(rows) == {"a", "b"}
    cheat = rows["a"]
    assert (cheat.playbook, cheat.setup_type) == ("cheat", s.CHEAT_3C)
    setup = cheat.criteria_results["setup"]
    assert set(setup) >= {"pivot", "stop", "stop_pct", "footprint", "dryup_mean_vr", "stage"}
    assert setup["setup_tags"] == [s.CHEAT_3C]
    assert cheat.criteria_results[scan.WINDOW] == {
        "found_on": BARS["date"].max().isoformat(),
        "session": 1,
        "sessions": 5,
    }
    assert cheat.valid_until == BARS["date"].max() + timedelta(days=5)


def test_scan_equals_the_backtests_setup_list() -> None:
    """The live scan (no memo) and the backtest's (a run memo kept from the night before) list
    the same setups, with the same fields, for the same date and bars."""
    live = scan.scan_bot(CONFIG, _inputs(BARS))
    memo: dict[Any, Any] = {}
    night_before = BARS.filter(pl.col("date") < BARS["date"].max())
    scan.scan_bot(CONFIG, _inputs(night_before, memo=memo))
    backtest = scan.scan_bot(CONFIG, _inputs(BARS, memo=memo))
    assert memo  # the earlier bases were cached on the night before
    assert live.rows == backtest.rows
    assert live.funnel == backtest.funnel


def _first(bars: pl.DataFrame, config: StrategyConfig = CONFIG) -> scan.WatchRow:
    (row,) = [r for r in scan.scan_bot(config, _inputs(bars)).rows if r.instrument_id == "a"]
    return row


def _found(
    row: scan.WatchRow,
    first: date,
    status: str = scan.OPEN,
    **results: Any,  # noqa: ANN401
) -> scan.SetupWindow:
    """`row`'s setup window as stored, first found on `first`."""
    stored = {**row.criteria_results, **results}
    stored[scan.WINDOW] = {**stored[scan.WINDOW], "found_on": first.isoformat()}
    pivot_on = date.fromisoformat(stored["pivot_date"])
    return scan.SetupWindow(
        row.playbook, row.instrument_id, pivot_on, first, status, row.score, stored
    )


def _a(result: scan.ScanResult) -> list[scan.WatchRow]:
    return [r for r in result.rows if r.instrument_id == "a"]


def _windows(result: scan.ScanResult) -> dict[str, str]:
    return {f"{w.playbook}:{w.instrument_id}": w.status for w in result.windows}


def test_a_setup_found_again_keeps_its_first_night() -> None:
    first = _first(BARS)
    as_of = BARS["date"].max()
    again = scan.scan_bot(CONFIG, _inputs(BARS, windows=[_found(first, as_of - timedelta(4))]))
    (row,) = _a(again)
    assert row.criteria_results[scan.WINDOW]["session"] == 5  # D+5: its last session
    assert row.valid_until == as_of + timedelta(1)  # the trade date is its last session


def test_a_window_expires_and_is_never_armed_again() -> None:
    first = _first(BARS)
    as_of = BARS["date"].max()
    # night D+5: found again, but its window D+1..D+5 is over
    over = scan.scan_bot(CONFIG, _inputs(BARS, windows=[_found(first, as_of - timedelta(5))]))
    assert _a(over) == []
    assert _windows(over)["cheat:a"] == scan.EXPIRED
    # night D+6: the expired setup is found again with the same pivot and stays closed
    closed = _found(first, as_of - timedelta(6), scan.EXPIRED)
    later = scan.scan_bot(CONFIG, _inputs(BARS, windows=[closed]))
    assert _a(later) == []
    assert "cheat:a" not in _windows(later)
    # a regime-off night ended it instead: a setup found again starts a new window
    reset = _found(first, as_of - timedelta(6), scan.REGIME_OFF)
    (row,) = _a(scan.scan_bot(CONFIG, _inputs(BARS, windows=[reset])))
    assert row.criteria_results[scan.WINDOW]["session"] == 1


def test_a_regime_off_night_ends_every_open_window() -> None:
    windows = [_found(_first(BARS), BARS["date"].max())]
    off = _inputs(BARS, windows=windows, regime_closes=[D(n) for n in range(60, 0, -1)])
    result = scan.scan_bot(CONFIG, off)
    assert (result.gate_on, _windows(result)) == (False, {"cheat:a": scan.REGIME_OFF})


def test_a_triggered_setup_is_not_armed_again() -> None:
    first = _first(BARS)
    window = _found(first, BARS["date"].max() - timedelta(1))
    result = scan.scan_bot(CONFIG, _inputs(BARS, windows=[window], triggered={window.key}))
    assert _a(result) == []
    assert _windows(result)["cheat:a"] == scan.TRIGGERED


def _later(close: float) -> pl.DataFrame:
    a = frame([*CHEAT, close], [*quiet(15, len(CHEAT)), 5000.0])
    return pl.concat(
        [
            a.with_columns(pl.lit("a").alias("instrument_id")),
            frame([50.0] * len(a), [1000.0] * len(a)).with_columns(
                pl.lit("c").alias("instrument_id")
            ),
        ]
    )


NIGHT = _bars({"a": CHEAT, "c": [50.0] * len(CHEAT)}, {"a": 15, "c": 0})


def _carried(
    close: float,
    config: StrategyConfig = CONFIG,
    **changes: Any,  # noqa: ANN401
) -> scan.ScanResult:
    """The night after the cheat setup of `a` was found, a night that adds `close` on heavy
    volume (the pause no longer dries up, so the scan does not find it again)."""
    windows = [_found(_first(NIGHT, config), NIGHT["date"].max(), **changes)]
    return scan.scan_bot(config, _inputs(_later(close), windows=windows))


def test_a_setup_not_found_again_is_carried() -> None:
    result = _carried(81.0)  # above its cancel level, under its pivot
    (row,) = result.rows
    assert row.criteria_results["carried"] is True
    assert row.criteria_results[scan.WINDOW]["session"] == 2
    assert row.pivot == D("82.41")
    assert row.valid_until == _later(81.0)["date"].max() + timedelta(4)  # D+5
    assert _windows(result) == {"cheat:a": scan.OPEN}


def test_a_close_below_the_cancel_level_cancels() -> None:
    result = _carried(78.0)  # 0.5% under the pause's low of 79.202
    assert (result.rows, _windows(result)) == ((), {"cheat:a": scan.CANCELLED})


def test_the_cancel_level_is_the_contraction_low_less_the_stop_buffer() -> None:
    playbook = CONFIG.playbooks[0]
    assert scan.cancel_level(playbook, D("80")) == D("79.6")
    # a playbook that stops a fixed 2% under the entry plans its stop at ~80.76, yet a close
    # of 80 is above the contraction's cancel level (~78.81): the setup is carried
    two_pct = {"rule": "pct_below_entry", "pct": 2}
    playbooks = [{**p, "stop": two_pct} for p in S1_SETUPS["playbooks"]]
    config = parse_config({**S1_SETUPS, "playbooks": playbooks})
    (row,) = _carried(80.0, config).rows
    assert row.planned_stop is not None
    assert row.planned_stop > D(80)


def test_carried_tags_are_evaluated_again() -> None:
    stale = {"setup": {**_first(NIGHT).criteria_results["setup"], "early_leader": "stale"}}
    (row,) = _carried(81.0, **stale).rows
    setup = row.criteria_results["setup"]
    assert setup.get("early_leader") is None  # tonight's: no benchmark here, so no tag
    assert setup["stage"] is not None


def test_a_setup_failing_its_playbooks_own_universe_is_not_carried() -> None:
    own = [{"type": "min_price", "params": {"inr": 100}}]  # the close of 81 is under it
    playbooks = [{**p, "universe": own} for p in S1_SETUPS["playbooks"]]
    windows = [_found(_first(NIGHT), NIGHT["date"].max())]
    config = parse_config({**S1_SETUPS, "playbooks": playbooks})
    result = scan.scan_bot(config, _inputs(_later(81.0), windows=windows))
    assert (result.rows, _windows(result)) == ((), {"cheat:a": scan.CANCELLED})


def test_one_setup_per_instrument_keeps_the_others_windows() -> None:
    twice = [S1_SETUPS["playbooks"][0], {**S1_SETUPS["playbooks"][0], "name": "cheat2"}]
    config = parse_config({**S1_SETUPS, "playbooks": twice})
    tonight = scan.scan_bot(config, _inputs(NIGHT))
    assert [r.playbook for r in _a(tonight)] == ["cheat"]  # one row for trading
    assert _windows(tonight) == {"cheat:a": scan.OPEN, "cheat2:a": scan.OPEN}
    # the next night both are carried, each in its own window; one row again
    later = scan.scan_bot(config, _inputs(_later(81.0), windows=tonight.windows))
    assert [r.playbook for r in later.rows] == ["cheat"]
    assert _windows(later) == {"cheat:a": scan.OPEN, "cheat2:a": scan.OPEN}


def test_the_window_counts_the_calendars_sessions() -> None:
    # `a` has no bar on one session after it was found; the window counts the session anyway
    bars = _later(81.0)
    gap = bars["date"].max() - timedelta(1)
    a_missing = bars.filter(~((pl.col("instrument_id") == "a") & (pl.col("date") == gap)))
    assert scan.calendar(_inputs(a_missing))[-2] == gap
    assert scan.window_session(scan.calendar(_inputs(a_missing)), gap - timedelta(1)) == 3


def test_both_scan_windows_are_700_sessions() -> None:
    calendar = [date(2015, 1, 1) + timedelta(days=n) for n in range(750)]
    as_of = calendar[-10]
    live = scan.scan_window(calendar, as_of)
    assert (len(live), live[-1]) == (scan.SCAN_SESSIONS, as_of)
    bars = pl.DataFrame(
        {
            "instrument_id": ["x"] * len(calendar),
            "date": calendar,
            **{c: [1_000_000] * len(calendar) for c in ("open_e4", "high_e4", "low_e4")},
            "close_e4": [1_000_000] * len(calendar),
            "raw_close": [100.0] * len(calendar),
            "close": [100.0] * len(calendar),
        }
    )
    market = MarketData(
        tuple(calendar), bars, pl.DataFrame({"date": [], "instrument_id": []}), {}, ()
    )
    assert MarketView(market, "NIFTY 500").window(as_of).height == len(live)


def test_one_row_per_instrument_records_every_setup() -> None:
    a = scan.WatchRow(
        "cheat", "x", 1.0, {"setup": {"setup_type": "cheat_3c"}}, setup_type="cheat_3c"
    )
    b = replace(a, playbook="handle", setup_type="cup_handle", criteria_results={"setup": {}})
    (kept,) = scan.with_setup_tags(CONFIG, [b, a])
    assert (kept.playbook, kept.criteria_results["setup"]["setup_tags"]) == (
        "cheat",
        ["cheat_3c", "cup_handle"],
    )
    both = scan.with_setup_tags(
        parse_config({**S1_SETUPS, "one_setup_per_instrument": False}), [b, a]
    )
    assert len(both) == 2


def test_leader_rs_slot_order() -> None:
    rs = {"a": D(5), "b": D(9), "c": D(1), "d": None}
    leaders = {"c", "d"}
    ordered = order_slots(
        list(rs), "leader_rs", date(2021, 3, 1), lambda _: (0, D(0)), rs.get, leaders.__contains__
    )
    assert ordered == ["c", "d", "b", "a"]
    assert is_slot_order("leader_rs")
