"""The S1 book rules at their seams (owner spec A, B, C-G): the eligible RS rank, the history
trend template, the zigzag swings, the base geometry's rules, time compression, dry-up, relative
correction and the base count. One passing and one failing small series per rule; no run of the
loop or the scan."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import polars as pl
import pytest

from app.hat.data.features import ELIGIBLE_RS, eligible_rs
from app.hat.engine.criteria import evaluate
from app.hat.engine.criteria.base import EvalContext
from app.hat.engine.criteria.book import (
    TEMPLATE_CHECKS,
    BookVcpParams,
    Contraction,
    Geometry,
    book_vcp,
    count_start,
    dryup_fault,
    dryup_ratios,
    earlier_bases,
    footprint,
    gate_of,
    geometry_fault,
    index_depth_pct,
    relative_fault,
    shakeout,
    template_checks,
    time_fault,
    zigzag,
)
from app.hat.engine.criteria.setup import RsRankParams

D = Decimal
P = BookVcpParams()
START = date(2015, 1, 1)


def _days(n: int) -> list[date]:
    return [START + timedelta(days=i) for i in range(n)]


def _path(points: list[tuple[int, float]], start: float) -> list[float]:
    """Closes walking linearly from `start` to each (bars, level) in turn."""
    out, level = [], start
    for bars, target in points:
        step = (target - level) / bars
        out.extend(level + step * (i + 1) for i in range(bars))
        level = target
    return out


def _frame(closes: list[float], volume: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": _days(len(closes)),
            "high": [c * 1.005 for c in closes],
            "low": [c * 0.995 for c in closes],
            "close": closes,
            "volume": volume,
            "raw_close": closes,
        }
    )


def _ctx(frame: pl.DataFrame, benchmark: list[tuple[date, float]]) -> EvalContext:
    return EvalContext(as_of=frame["date"][-1], instrument_id="i", bars=frame, benchmark=benchmark)


# A base after a 300-bar advance: H0 100, contractions of ~21%, ~11% and ~5%, then a rise
# towards the pivot that confirms the last swing low.
BASE = [(10, 80.0), (10, 96.0), (8, 86.4), (8, 94.0), (5, 90.24), (12, 93.0)]


def _base_series(scale: float = 1.0) -> list[float]:
    return [(c * scale) for c in _path(BASE, 100.0)]


def _volumes(closes: list[float], quiet: tuple[int, int]) -> list[float]:
    return [300.0 if quiet[0] <= i <= quiet[1] else 1000.0 for i in range(len(closes))]


def _one_base() -> tuple[pl.DataFrame, int]:
    closes = _path([(300, 100.0)], 50.0) + _base_series()
    final_high = 300 + 10 + 10 + 8 + 8 - 1  # the bar of the 94.0 swing high
    return _frame(closes, _volumes(closes, (final_high, final_high + 5))), final_high


def _benchmark(frame: pl.DataFrame, dip: float) -> list[tuple[date, float]]:
    n = frame.height
    closes = [100.0] * n
    closes[n - 30] = 100.0 * (1 - dip / 100)
    return list(zip(frame["date"].to_list(), closes, strict=True))


# --- A: RS rank within the eligible universe -------------------------------------------------


def _universe() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "instrument_id": ["a", "b", "c", "x"],
            "rs_weighted": [0.1, 0.5, 0.9, 5.0],
            "rs_pct": [10.0, 50.0, 90.0, 99.0],
        }
    )


def test_eligible_rs_ranks_1_to_99_within_the_eligible_only() -> None:
    ranked = eligible_rs(_universe(), ["a", "b", "c"])
    assert ranked[ELIGIBLE_RS].to_list() == [1.0, 50.0, 99.0, None]
    assert eligible_rs(_universe(), ["c"])[ELIGIBLE_RS].to_list() == [None, None, 99.0, None]


@pytest.mark.parametrize(("instrument", "passed"), [("c", True), ("a", False)])
def test_rs_rank_eligible_basis(instrument: str, passed: bool) -> None:
    ranked = eligible_rs(_universe(), ["a", "b", "c"])
    ctx = EvalContext(as_of=START, instrument_id=instrument, bars=pl.DataFrame(), universe=ranked)
    result = evaluate("rs_rank", {"min_percentile": 70.0, "basis": "eligible"}, ctx)
    assert result.passed is passed
    # the stored configs' rank (every NSE equity) is unchanged and keeps its canonical form
    assert evaluate("rs_rank", {"min_percentile": 70.0}, ctx).value == (
        90.0 if instrument == "c" else 10.0
    )
    assert "basis" not in RsRankParams().model_dump()


# --- B: the trend template's checks on the history -----------------------------------------


@pytest.mark.parametrize(("start", "end", "passed"), [(50.0, 150.0, True), (150.0, 50.0, False)])
def test_template_checks_each(start: float, end: float, passed: bool) -> None:
    closes = _path([(300, end)], start)
    high, low = [c * 1.005 for c in closes], [c * 0.995 for c in closes]
    evaluable, checks, below = template_checks(high, low, closes)
    assert evaluable[-1]
    assert not evaluable[250]
    assert set(checks) == set(TEMPLATE_CHECKS)
    for name, values in checks.items():
        assert bool(values[-1]) is passed, name
    assert bool(below[-1]) is not passed
    # the hard criteria read the same verdicts on the last bar
    frame = _frame(closes, [1000.0] * len(closes))
    ctx = EvalContext(as_of=frame["date"][-1], instrument_id="i", bars=frame)
    for code, params in (
        ("sma_above", {"a": "close", "b": 150}),
        ("sma_above", {"a": 150, "b": 200}),
        ("sma_rising", {"sma": 200, "lookback_days": 21}),
        ("sma_above", {"a": 50, "b": 200}),
        ("pct_from_52w_low", {"min_pct": 30.0}),
        ("pct_from_52w_high", {"max_pct_below": 25.0}),
    ):
        assert evaluate(code, params, ctx).passed is passed, code


# --- C: swings and geometry -----------------------------------------------------------------


def test_zigzag_confirms_swings_past_the_threshold() -> None:
    high = [100.0, 95.0, 90.0, 85.0, 89.0, 92.0, 88.0, 86.0, 90.0]
    low = [h - 1 for h in high]
    swings = zigzag(high, low, [0.03] * len(high), 0, len(high) - 1)
    # H0, the low of 84 confirmed by the 88-high bar, the high of 92 confirmed by 87's low,
    # the low of 85 confirmed by the 90-high bar
    assert [(s.high, s.index) for s in swings] == [
        (True, 0),
        (False, 3),
        (True, 5),
        (False, 7),
    ]


def test_zigzag_ignores_moves_under_the_threshold() -> None:
    high = [100.0, 99.0, 98.0, 99.5, 98.5, 99.0]
    low = [h - 0.5 for h in high]
    assert len(zigzag(high, low, [0.03] * len(high), 0, len(high) - 1)) == 1


def _geo(depths: list[float], h0: int = 0, final_sessions: int = 4) -> Geometry:
    out, at = [], h0
    for n, depth in enumerate(depths):
        end = at + (final_sessions - 1 if n == len(depths) - 1 else 5)
        out.append(Contraction(at, end, D(100), D(100) - D(str(depth))))
        at = end + 3
    return Geometry(h0, tuple(out), 0)


@pytest.mark.parametrize(
    ("depths", "end", "reason"),
    [
        ([20, 10, 5], 40, None),
        ([20, 10, 5], 13, "base_too_short"),
        ([20, 10, 5], 400, "base_too_long"),
        ([20], 40, "too_few_contractions"),
        ([30, 20, 14, 10, 7, 5, 3], 60, "too_many_contractions"),
        ([8, 5], 40, "first_depth_out_of_band"),
        ([40, 20], 40, "first_depth_out_of_band"),
        ([20, 16], 40, "depth_ratio_too_wide"),
        ([30, 12], 40, "final_depth_too_deep"),
    ],
)
def test_geometry_rules(depths: list[float], end: int, reason: str | None) -> None:
    fault = geometry_fault(_geo(depths), end, P)
    assert (fault[0] if fault else None) == reason


def test_final_contraction_length() -> None:
    assert geometry_fault(_geo([20, 5], final_sessions=3), 40, P) is None
    assert geometry_fault(_geo([20, 5], final_sessions=2), 40, P) == (
        "final_contraction_too_short",
        2.0,
    )


def test_swing_high_above_h0_is_rejected() -> None:
    geo = _geo([20, 10])
    assert geometry_fault(Geometry(0, geo.contractions, 1), 40, P) == ("swing_high_above_h0", 1.0)


def test_footprint_and_shakeout() -> None:
    geo = _geo([22.4, 10, 2.5])
    assert footprint(40, geo.contractions) == "8W 22/3 3T"
    assert not shakeout(geo.contractions)  # every low 100 - depth from the same high
    undercut = (Contraction(0, 5, D(100), D(80)), Contraction(8, 12, D(90), D(78)))
    assert shakeout(undercut)


# --- D, E, F ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "reason"),
    [(20, 12, None), (40, 15, "time_not_compressed"), (10, 8, "too_soon_after_low")],
)
def test_time_compression(before: int, after: int, reason: str | None) -> None:
    assert time_fault(before, after, P) == reason


def test_dryup_both_conditions() -> None:
    final = Contraction(50, 54, D(100), D(95))
    volume: list[float | None] = [1000.0] * 50 + [400.0, 600.0, 450.0, 450.0, 600.0]
    mean, lowest = dryup_ratios(volume, final, 50) or (D(0), D(0))
    assert dryup_fault(mean, lowest, P) is None  # mean 0.5, low 0.4 of the 50-day mean
    assert dryup_fault(D("0.75"), D("0.4"), P) == "no_volume_dryup"
    assert dryup_fault(D("0.6"), D("0.5"), P) == "no_dryup_session"
    assert dryup_ratios(volume, Contraction(10, 14, D(100), D(95)), 50) is None


def test_relative_correction() -> None:
    assert index_depth_pct([100, 90, 95, 80, 85]) == D(20)
    assert not relative_fault(D(30), D(20), P)  # within 2.5x the index's fall
    assert relative_fault(D(30), D(10), P)
    assert not relative_fault(D(14), D(2), P)  # deep only against the index, under 15%


# --- G: base count ---------------------------------------------------------------------------


def test_count_start_from_stage2_and_reset_below_sma200() -> None:
    evaluable = np.array([False] * 5 + [True] * 100)
    passes = np.array([False] * 70 + [True] * 35)
    below = np.zeros(105, dtype=bool)
    assert count_start(evaluable, passes, below, 104, 60) == (70, False)
    below[90] = True
    assert count_start(evaluable, passes, below, 104, 60) == (90, False)
    # fewer than 60 failing sessions before the pass: no stage-2 start in the history
    assert count_start(evaluable, passes, np.zeros(105, dtype=bool), 104, 70) == (5, True)


def test_earlier_bases_count_broken_pivots_only() -> None:
    close = [10.0] * 10 + [12.0] + [10.0] * 9
    high = [11.0] * 20
    bases = {2: (0, 3), 3: (0, 3), 12: (11, 13), 15: (14, 15)}
    # H0 0's pivot (11) is broken by the 12 close; H0 11's is not; 14 is the current base
    assert earlier_bases(bases, close, high, 0, 19, current_h0=14) == 1
    assert earlier_bases(bases, close, high, 11, 19, current_h0=14) == 0  # reset after it


# --- the criterion over a series --------------------------------------------------------------


def test_book_vcp_passes_a_book_base() -> None:
    frame, final_high = _one_base()
    result = book_vcp(_ctx(frame, _benchmark(frame, 10)), P)
    assert result.passed, result.reason
    extras = result.extras
    assert extras["contraction_count"] == 3
    assert extras["pivot"] == pytest.approx(94.0 * 1.005)
    assert extras["final_contraction_low"] == pytest.approx(90.24 * 0.995)
    assert extras["footprint"] == "11W 21/5 3T"
    assert extras["base_number"] == 1
    assert extras["stage2_start_unknown"] is True
    assert extras["shakeout"] is False
    assert extras["dryup_min_ratio"] < 0.5
    assert frame["date"][final_high].isoformat() == extras["contractions"][-1]["start"]


def test_book_vcp_fails_against_a_flat_index() -> None:
    frame, _ = _one_base()
    result = book_vcp(_ctx(frame, _benchmark(frame, 1)), P)
    assert (result.passed, result.reason) == (False, "correction_too_deep_vs_index")
    assert gate_of(result.reason) == "relative_correction"


def test_book_vcp_counts_an_earlier_broken_base() -> None:
    closes = _path([(300, 100.0)], 50.0) + _base_series()
    closes += _path([(20, 130.0)], closes[-1]) + _base_series(1.3)
    final_high = len(closes) - 6 - 12
    frame = _frame(closes, _volumes(closes, (final_high, final_high + 5)))
    ctx = _ctx(frame, _benchmark(frame, 10))
    memo: dict = {}
    ctx.memo = memo
    assert book_vcp(ctx, P).extras["base_number"] == 2
    assert memo  # the earlier dates' geometry is kept for the next night
    one = BookVcpParams(max_base_number=1)
    assert book_vcp(ctx, one).reason == "late_stage_base"


def test_book_vcp_switched_off_rules_need_no_data() -> None:
    frame, _ = _one_base()
    frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias("volume"))
    assert book_vcp(_ctx(frame, []), P).reason == "no_volume_data"
    off = BookVcpParams(dryup_mean_max=None, dryup_session_max=None, rc_max_ratio=None)
    result = book_vcp(_ctx(frame, []), off)
    assert result.passed, result.reason
    assert "dryup_mean_ratio" not in result.extras
    assert "index_depth_pct" not in result.extras
