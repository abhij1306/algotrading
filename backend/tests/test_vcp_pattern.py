"""Unit tests for the pure VCP pattern functions on synthetic price series (no DB)."""

import numpy as np
import pandas as pd
import pytest

from app.services.vcp import pattern
from app.services.vcp.pattern import Contraction

# Base anchors (bar index, close) for three tightening pullbacks: about 18%, 9% and 4%.
TIGHTENING = [(0, 100.0), (6, 82.0), (12, 96.0), (17, 88.3), (22, 93.0), (26, 90.2), (59, 91.0)]
# The second pullback (to 80) is deeper than the first (to 90): a failed contraction.
WIDENING = [(0, 100.0), (6, 90.0), (12, 98.0), (17, 80.0), (22, 86.0), (26, 84.0), (59, 85.0)]


def _segments(anchors: list[tuple[int, float]]) -> list[float]:
    out: list[float] = []
    for (i0, p0), (i1, p1) in zip(anchors, anchors[1:], strict=False):
        out.extend(np.linspace(p0, p1, i1 - i0 + 1)[:-1])
    out.append(anchors[-1][1])
    return out


def bars(closes: list[float], volume: list[float] | None = None) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame(
        {
            "symbol": ["VCPX"] * n,
            "close": closes,
            "close_ref": closes,
            "high": [c * 1.005 for c in closes],
            "low": [c * 0.995 for c in closes],
            "volume": volume if volume is not None else [1_000_000.0] * n,
        }
    )


def vcp_frame(base_anchors: list[tuple[int, float]] = TIGHTENING) -> pd.DataFrame:
    """300 sessions: a stage-2 uptrend, then a 60-day base on drying-up volume."""
    trend = list(np.linspace(40.0, 85.0, 230)) + list(np.linspace(86.5, 98.5, 10))
    volume = (
        [1_000_000.0] * 240
        + list(np.linspace(900_000, 500_000, 22))
        + [80_000.0] * 5
        + [120_000.0] * 33
    )
    volume[-1] = 200_000.0
    return bars(trend + _segments(base_anchors), volume)


# --- analyze_pattern ----------------------------------------------------------------


def test_clean_vcp_is_a_grade_a_setup() -> None:
    result = pattern.analyze_pattern(vcp_frame(), 92)

    assert result is not None
    assert result.grade == "A"
    assert result.contraction_depths == [18.41, 8.94, 3.98]
    assert result.pivot_high == pytest.approx(93.465)
    assert result.final_contraction_low == pytest.approx(89.749)
    assert result.stop_level == 89.66
    assert result.days_in_base == 60
    assert result.base_depth_pct == pytest.approx(18.0)
    assert result.stage2.conditions_met == 5
    assert result.volume.dry_up_pct == pytest.approx(34.1964, abs=1e-4)
    assert result.breakout.is_breakout is False
    assert result.latest_close == 91.0


def test_widening_contraction_is_rejected() -> None:
    assert pattern.analyze_pattern(vcp_frame(WIDENING), 92) is None


def test_insufficient_history_is_rejected() -> None:
    assert pattern.analyze_pattern(vcp_frame().tail(259), 92) is None


def test_relative_strength_below_80_is_rejected() -> None:
    assert pattern.analyze_pattern(vcp_frame(), 79) is None


# --- relative strength -------------------------------------------------------------


def test_percentile_rating_scales_rank_to_0_99() -> None:
    ratings = pattern.percentile_rating(pd.Series({"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0}))

    assert ratings.to_dict() == {"a": 25, "b": 50, "c": 74, "d": 99}


def test_rs_map_ranks_stronger_symbol_higher_and_skips_short_history() -> None:
    frames = {
        "FAST": bars(list(np.linspace(50.0, 150.0, 253))),
        "SLOW": bars(list(np.linspace(100.0, 110.0, 253))),
        "NEW": bars(list(np.linspace(100.0, 200.0, 252))),
    }

    assert pattern.compute_rs_map(frames) == {"FAST": 99, "SLOW": 50}


# --- stage 2 -----------------------------------------------------------------------


def test_stage2_uptrend_meets_all_conditions() -> None:
    metrics = pattern.stage2_metrics(bars(list(np.linspace(50.0, 100.0, 260))))

    assert metrics.conditions_met == 5
    assert metrics.high_52 == 100.0


def test_stage2_downtrend_meets_none() -> None:
    metrics = pattern.stage2_metrics(bars(list(np.linspace(100.0, 50.0, 260))))

    assert metrics.conditions_met == 0


def test_stage2_short_history_reports_zero_averages() -> None:
    metrics = pattern.stage2_metrics(bars([10.0] * 30))

    assert metrics.averages() == {"ma_50": 0.0, "ma_150": 0.0, "ma_200": 0.0, "high_52": 0.0}


# --- pivots and base ---------------------------------------------------------------


def test_pivot_points_find_strict_local_extremes() -> None:
    closes = [10.0, 11.0, 12.0, 15.0, 12.0, 11.0, 10.0, 8.0, 10.0, 11.0, 12.0]

    assert pattern.pivot_points(bars(closes), window=3) == ([3], [7])


def test_base_window_starts_at_60_day_high() -> None:
    base = pattern.base_window(vcp_frame())

    assert base is not None
    assert len(base.frame) == 60
    assert base.depth_pct == pytest.approx(18.0)


def test_base_shorter_than_15_bars_is_rejected() -> None:
    closes = [90.0] * 50 + [100.0] + [95.0] * 9

    assert pattern.base_window(bars(closes)) is None


def test_base_deeper_than_30_pct_is_rejected() -> None:
    closes = [100.0] + [65.0] * 59

    assert pattern.base_window(bars(closes)) is None


def test_overhead_resistance() -> None:
    clear = bars([100.0] * 10)
    held_five_bars = bars([100.0] * 10 + [102.0] * 5 + [100.0] * 10)
    held_four_bars = bars([90.0] * 10 + [102.0] * 4 + [90.0] * 10)

    assert pattern.has_no_overhead_resistance(clear, 0.0) is False
    assert pattern.has_no_overhead_resistance(clear, 101.0) is True
    assert pattern.has_no_overhead_resistance(held_five_bars, 101.0) is False
    assert pattern.has_no_overhead_resistance(held_four_bars, 101.0) is True


# --- contractions ------------------------------------------------------------------


def test_find_contractions_walks_pivot_high_to_pivot_low() -> None:
    base = pattern.base_window(vcp_frame())
    assert base is not None

    contractions = pattern.find_contractions(base.frame)

    assert [(c.high_idx, c.low_idx) for c in contractions] == [(0, 6), (12, 17), (22, 26)]
    assert [round(c.depth_pct, 2) for c in contractions] == [18.41, 8.94, 3.98]


def test_find_contractions_on_widening_base() -> None:
    base = pattern.base_window(vcp_frame(WIDENING))
    assert base is not None

    depths = [round(c.depth_pct, 2) for c in pattern.find_contractions(base.frame)]

    assert depths == [10.45, 19.18, 3.3]
    assert pattern.contractions_tighten(depths) is False


@pytest.mark.parametrize(
    ("depths", "expected"),
    [
        ([18.0, 9.0, 4.0], True),
        ([20.0, 8.0], True),
        ([24.0, 14.0, 9.0, 5.0], True),
        ([8.0], False),  # one contraction
        ([20.0, 15.0, 10.0, 6.0, 3.0], False),  # five contractions
        ([26.0, 10.0], False),  # first deeper than 25%
        ([20.0, 16.0, 8.0], False),  # second deeper than 15%
        ([20.0, 12.0, 11.0], False),  # last deeper than 10%
        ([20.0, 8.0, 8.0], False),  # not strictly shallower
    ],
)
def test_contractions_tighten(depths: list[float], expected: bool) -> None:
    assert pattern.contractions_tighten(depths) is expected


# --- volume dry-up -----------------------------------------------------------------

FINAL = Contraction(high_idx=5, high_price=100.0, low_idx=9, low_price=95.0, depth_pct=5.0)


def _volume_case(final_volume: float, early_volume: float = 100.0) -> pd.DataFrame:
    """Base of 10 bars; the last five (the final contraction) trade `final_volume`."""
    return bars([100.0] * 10, [early_volume] * 5 + [final_volume] * 5)


def test_volume_dry_up_at_exactly_40_pct_passes() -> None:
    frame = bars([100.0] * 50, [100.0] * 50)

    result = pattern.volume_dry_up(frame, _volume_case(40.0), FINAL)

    assert result is not None
    assert result.avg_volume_50 == 100.0
    assert result.dry_up_pct == 40.0
    assert result.slope == pytest.approx(-750 / 82.5)


def test_volume_dry_up_above_40_pct_is_rejected() -> None:
    frame = bars([100.0] * 50, [100.0] * 50)

    assert pattern.volume_dry_up(frame, _volume_case(40.1), FINAL) is None


def test_volume_rising_across_base_is_rejected() -> None:
    frame = bars([100.0] * 50, [100.0] * 50)

    assert pattern.volume_dry_up(frame, _volume_case(40.0, early_volume=10.0), FINAL) is None


# --- breakout and grade ------------------------------------------------------------


def test_breakout_needs_close_above_pivot_on_volume_near_high() -> None:
    day = pd.Series({"close": 104.0, "high": 105.0, "low": 101.0, "volume": 150.0})

    assert pattern.breakout_state(day, 103.0, 100.0) == pattern.Breakout(True, 1.5, 0.75)
    assert pattern.breakout_state(day, 104.0, 100.0).is_breakout is False
    assert pattern.breakout_state(day, 103.0, 101.0).is_breakout is False


@pytest.mark.parametrize(
    ("rs", "depth", "dry_up", "is_breakout", "mult", "expected"),
    [
        (90, 5.0, 40.0, False, 0.0, "A"),
        (95, 4.0, 30.0, True, 2.0, "A"),
        (95, 4.0, 30.0, True, 1.9, "B"),  # breakout volume below the A-grade 2x
        (89, 5.0, 40.0, False, 0.0, "B"),
        (85, 10.0, 60.0, False, 0.0, "B"),
        (85, 10.1, 60.0, False, 0.0, "C"),
        (79, 4.0, 30.0, False, 0.0, "C"),
    ],
)
def test_grade(
    rs: int, depth: float, dry_up: float, is_breakout: bool, mult: float, expected: str
) -> None:
    assert pattern.grade(rs, depth, dry_up, is_breakout, mult) == expected
