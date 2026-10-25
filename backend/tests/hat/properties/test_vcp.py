"""The VCP detector's pure parts on small hand-built price lists: the contraction chain over the
whole base, the most recent counted contractions, the noise floor and the score at the limits."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.hat.engine.criteria.vcp import (
    VcpParams,
    build_contractions,
    contraction_chain,
    pivot_points,
    recent_contractions,
    vcp_score,
)

LEG = 4  # bars per leg: a turning point is strictly beyond 3 neighbours on each side


def zigzag(turns: list[float]) -> tuple[list[float], list[float]]:
    """(highs, lows) of a path through `turns` (peak, trough, peak, ...), `LEG` bars per leg,
    each bar's high and low 0.5 either side of its price."""
    prices: list[float] = []
    for a, b in zip(turns, turns[1:], strict=False):
        prices += [a + (b - a) * i / LEG for i in range(LEG)]
    prices.append(turns[-1])
    return [p + 0.5 for p in prices], [p - 0.5 for p in prices]


def depths(turns: list[float], **params: object) -> list[float]:
    highs, lows = zigzag(turns)
    chain = contraction_chain(highs, lows, 3)
    found = recent_contractions(build_contractions(highs, lows, chain), VcpParams(**params))
    return [round(float(c.depth_pct), 1) for c in found]


# Eight pullbacks from 100: five loose ones, then 9%, 6% and 4% (the last three tighten).
EIGHT = [100, 80, 100, 85, 100, 82, 100, 88, 100, 84, 100, 92, 100, 95, 100, 97, 100]


def test_chain_covers_the_whole_base() -> None:
    """Every pullback of the base is in the chain, not only the first few."""
    highs, lows = zigzag(EIGHT)
    assert len(contraction_chain(highs, lows, 3)) == 8


def test_judges_the_most_recent_contractions() -> None:
    """With at most 3 counted, the pattern is the last three pullbacks (9, 6, 4%), so the final
    contraction and its pivot are the latest ones."""
    assert depths(EIGHT, contractions=[2, 3]) == [9.0, 6.0, 4.0]
    assert len(depths(EIGHT)) == 6  # the default cap, the latest six


def test_min_depth_drops_noise_wiggles() -> None:
    """A 2% wiggle between two real pullbacks is not a contraction once the floor is set."""
    turns = [100, 88, 100, 99, 100, 94, 100]
    assert depths(turns) == [12.9, 2.0, 7.0]
    assert depths(turns, min_depth_pct=3) == [12.9, 7.0]


def test_pivot_window_is_the_swing_definition() -> None:
    """A trough 4 bars from the ends is a pivot at window 3, not at window 5, which needs 5 bars
    on each side."""
    highs, lows = zigzag([100, 90, 100, 90, 100])
    assert pivot_points(highs, lows, 3) == ([8], [4, 12])
    assert pivot_points(highs, lows, 5) == ([8], [])


def test_score_at_the_pass_limits_is_the_floor() -> None:
    """Each tightness sub-score is 1 at the tightest value and `score_floor` exactly at its limit,
    so a pattern that just passes scores (1 + 3 x floor) / 4, not 1/4."""
    params = VcpParams()
    assert vcp_score(3, 0.0, 0.0, 0.0, params) == 1.0
    assert vcp_score(3, 0.75, 10.0, 0.7, params) == pytest.approx(0.625)
    assert vcp_score(3, 0.75, 10.0, 0.7, VcpParams(score_floor=0)) == pytest.approx(0.25)
    assert vcp_score(3, 0.8, 10.0, 0.7, params) == pytest.approx(0.5)  # ratio beyond its limit


def test_new_params_at_default_stay_out_of_the_canonical_form() -> None:
    """A stored config written before these parameters keeps its hash."""
    dumped = VcpParams().model_dump(mode="json")
    for key in ("pivot_window", "min_base_bars", "dryup_baseline_days", "min_depth_pct"):
        assert key not in dumped
    assert VcpParams(min_depth_pct=3).model_dump(mode="json")["min_depth_pct"] == 3


def test_lookback_shorter_than_min_base_is_refused() -> None:
    with pytest.raises(ValueError, match="lookback_days_below_min_base_bars"):
        VcpParams(lookback_days=20, min_base_bars=30)


def test_contraction_depth_is_exact() -> None:
    highs, lows = zigzag([100, 90, 100])
    (found,) = build_contractions(highs, lows, contraction_chain(highs, lows, 3))
    assert found.depth_pct == (Decimal("100.5") - Decimal("89.5")) / Decimal("100.5") * 100
