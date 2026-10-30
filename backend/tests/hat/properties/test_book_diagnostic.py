"""The audit view of `book_vcp` (`book_diagnostic`): each rule of C, D, E and F on its own, one
pass and one fail per rule, and its agreement with the criterion's first-fault order."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.hat.engine.criteria.book import (
    BookVcpParams,
    Contraction,
    Geometry,
    geometry_fault,
    swing_thresholds,
)
from app.hat.engine.criteria.book_diagnostic import (
    GEOMETRY_RULES,
    GeometryCheck,
    geometry_check,
    geometry_rules,
    later_check,
)

D = Decimal
P = BookVcpParams()


def _contractions(
    depths: list[float], final_sessions: int = 4, start: int = 0
) -> list[Contraction]:
    out, at = [], start
    for n, depth in enumerate(depths):
        end = at + (final_sessions - 1 if n == len(depths) - 1 else 5)
        out.append(Contraction(at, end, D(100), D(100) - D(str(depth))))
        at = end + 3
    return out


def _failed(depths: list[float], end: int = 40, above: int = 0, final: int = 4) -> list[str]:
    rules = geometry_rules(0, _contractions(depths, final), above, end, P)
    return [r for r in GEOMETRY_RULES if not rules[r]]


@pytest.mark.parametrize(
    ("depths", "end", "above", "final", "failed"),
    [
        ([20, 10, 5], 40, 0, 4, []),
        ([20, 10, 5], 13, 0, 4, ["base_length"]),
        ([20, 10, 5], 400, 0, 4, ["base_length"]),
        ([20, 10, 5], 40, 1, 4, ["swing_high_above_h0"]),
        ([8], 40, 0, 4, ["contraction_count", "first_depth_band"]),
        ([30, 20, 14, 10, 7, 5, 3], 60, 0, 4, ["contraction_count"]),
        ([40, 20, 8], 40, 0, 4, ["first_depth_band"]),
        ([20, 16, 8], 40, 0, 4, ["depth_ratio"]),
        ([30, 12], 40, 0, 4, ["final_depth"]),
        ([20, 5], 40, 0, 2, ["final_sessions"]),
        # independent: a too-shallow first and a too-wide ratio both count, no short-circuit
        ([8, 7], 13, 0, 4, ["base_length", "first_depth_band", "depth_ratio"]),
        ([], 40, 0, 4, ["contraction_count", "first_depth_band", "final_depth", "final_sessions"]),
    ],
)
def test_each_geometry_rule(
    depths: list[float], end: int, above: int, final: int, failed: list[str]
) -> None:
    assert _failed(depths, end, above, final) == failed


@pytest.mark.parametrize(
    ("depths", "end", "above", "final"),
    [([20, 10, 5], 40, 0, 4), ([20, 16, 8], 13, 0, 4), ([30, 12], 40, 1, 2), ([8], 400, 0, 4)],
)
def test_first_failed_rule_is_the_criterion_fault(
    depths: list[float], end: int, above: int, final: int
) -> None:
    contractions = _contractions(depths, final)
    fault = geometry_fault(Geometry(0, tuple(contractions), above), end, P)
    failed = _failed(depths, end, above, final)
    assert (fault is None) == (not failed)
    by_reason = {
        "base_too_short": "base_length",
        "base_too_long": "base_length",
        "swing_high_above_h0": "swing_high_above_h0",
        "too_few_contractions": "contraction_count",
        "too_many_contractions": "contraction_count",
        "first_depth_out_of_band": "first_depth_band",
        "depth_ratio_too_wide": "depth_ratio",
        "final_depth_too_deep": "final_depth",
        "final_contraction_too_short": "final_sessions",
    }
    assert fault is None or by_reason[fault[0]] == failed[0]


def test_geometry_check_on_a_series() -> None:
    # H0 100, a 20% fall, back to 96, a 9% fall, then a rise under 3%: two contractions
    high = [100.0, 90.0, 80.0, 88.0, 96.0, 92.0, 87.36, 92.0, 95.0, 95.5, 95.0, 95.5]
    low = [h - 0.1 for h in high]
    closes = high
    threshold = swing_thresholds(high, low, closes, BookVcpParams(swing_atr_multiple=0))
    check = geometry_check(high, low, threshold, len(high) - 1, P)
    assert check is not None
    assert [c.end for c in check.contractions] == [2, 6]
    assert check.failed == ("base_length",)  # 12 sessions, under 15
    # no 10% fall from the highest high: no base
    flat = [100.0, 95.0, 97.0, 96.0]
    assert geometry_check(flat, flat, [0.03] * 4, 3, P) is None


def _geo(depths: list[float], h0: int = 0) -> GeometryCheck:
    contractions = tuple(_contractions(depths, start=h0))
    return GeometryCheck(h0, (), contractions, dict.fromkeys(GEOMETRY_RULES, True))


def test_later_rules_each() -> None:
    geo = _geo([20, 10, 5], h0=50)  # the final contraction is bars 66..69
    low = [100.0] * 100
    low[60] = 80.0  # the deepest low, 10 sessions after H0
    volume: list[float | None] = [1000.0] * 100
    volume[66:70] = [400.0, 600.0, 450.0, 450.0]
    ok = later_check(geo, low, volume, 90, D(10), P)
    assert (ok.tc_low_sessions, ok.tc_after_sessions) == (10, 30)
    assert ok.failed == ()
    assert ok.rc_ratio == D(2)
    # 5 sessions after the low: under 10 and under half of the 35 sessions before it
    low[60], low[85] = 100.0, 80.0
    assert later_check(geo, low, volume, 90, D(10), P).failed == ("tc_ratio", "tc_min_sessions")
    low[85], low[60] = 100.0, 80.0
    loud: list[float | None] = [1000.0] * 100
    loud[66:70] = [900.0, 900.0, 900.0, 900.0]
    assert later_check(geo, low, loud, 90, D(10), P).failed == ("dryup_mean", "dryup_session")
    assert later_check(geo, low, volume, 90, D(2), P).failed == ("relative_correction",)
    assert later_check(geo, low, volume, 90, None, P).failed == ("relative_correction",)
    assert later_check(geo, low, [None] * 100, 90, D(10), P).failed == (
        "dryup_mean",
        "dryup_session",
    )
