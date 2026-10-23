"""PBT scoring.*: how criteria combine into a stage result.

The criteria are real: setup-stage `rs_rank` instances over a one-row universe whose `rs_pct` is
50. A hard instance passes when `min_percentile <= 50`; a score instance scores
`(50 - score_from) / (score_full - score_from)`. Oracles are written in `Fraction`s apart from the
code under test.
"""

import math
from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest
from hat.hat_criteria_support import Lcg, frame, make_rows, universe_frame
from hypothesis import given
from hypothesis import strategies as st

from app.hat.engine.criteria import EvalContext
from app.hat.engine.strategy.config import Instance
from app.hat.engine.strategy.scoring import (
    EMPTY_SCORE,
    numeric_levels,
    passes_threshold,
    weighted_mean,
)

RS = 50.0


def ctx(series: str = "EQ", rs: float = RS) -> EvalContext:
    return EvalContext(
        date(2024, 1, 2),
        "x",
        frame(make_rows(Lcg(1), 1)),
        universe=universe_frame([("x", {"rs_pct": rs})]),
        series=series,
    )


@st.composite
def instances(draw: st.DrawFn) -> list[Instance]:
    """Setup-stage `rs_rank` instances with distinct orders, in a random list order."""
    count = draw(st.integers(0, 6))
    orders = draw(st.permutations(range(10)).map(lambda p: list(p)[:count]))
    out = []
    for order in orders:
        hard = draw(st.booleans())
        params = (
            {"min_percentile": float(draw(st.sampled_from([0, 40, 50, 51, 90])))}
            if hard
            else {
                "score_from": float(draw(st.integers(0, 49))),
                "score_full": float(draw(st.integers(51, 100))),
            }
        )
        out.append(
            Instance(
                type="rs_rank",
                stage="setup",
                mode="hard" if hard else "score",
                weight=draw(st.sampled_from([0.5, 1.0, 2.0, 3.0])),
                params=params,
                enabled=draw(st.booleans()),
                order=order,
            )
        )
    return draw(st.permutations(out))


@given(
    pairs=st.lists(st.tuples(st.floats(1e-3, 1e3), st.floats(0, 1)), min_size=1, max_size=8),
    factor=st.floats(1e-2, 1e2),
    shuffle=st.randoms(use_true_random=False),
)
def test_weighted_mean(pairs: list, factor: float, shuffle) -> None:  # noqa: ANN001
    """The score is the weighted mean, inside [min s, max s], blind to the order of the criteria
    and to scaling every weight by one factor; no scored criterion gives 1.0."""
    got = weighted_mean(pairs)
    want = sum(Fraction(w) * Fraction(s) for w, s in pairs) / sum(Fraction(w) for w, _ in pairs)
    assert got == pytest.approx(float(want), abs=1e-12)
    assert min(s for _, s in pairs) <= got <= max(s for _, s in pairs)
    mixed = list(pairs)
    shuffle.shuffle(mixed)
    assert weighted_mean(mixed) == pytest.approx(got, abs=1e-12)
    assert weighted_mean([(w * factor, s) for w, s in pairs]) == pytest.approx(got, abs=1e-12)
    assert weighted_mean([]) == EMPTY_SCORE == 1.0


@given(hard_ok=st.booleans(), score=st.floats(0, 1), threshold=st.floats(0, 1))
def test_threshold_gate(hard_ok: bool, score: float, threshold: float) -> None:
    """A symbol passes iff every hard criterion passes and the score reaches the threshold."""
    assert passes_threshold(hard_ok, score, threshold) == (hard_ok and score >= threshold)
    assert passes_threshold(True, threshold, threshold)  # exactly at the bar passes


CONFIG = {
    "bot": "probe",
    "universe": [{"type": "series_in", "params": {"series": ["EQ"]}}],
    "regime": {
        "index": "NIFTY 500",
        "rule": "close_above_sma_and_rising",
        "params": {"sma": 50, "rising_days": 10},
    },
    "setup": {
        "threshold": 0.5,
        "criteria": [{"type": "rs_rank", "mode": "score", "weight": 1, "params": {}}],
    },
    "stop": {"rule": "low_of_day"},
    "exits": [{"rule": "hard_stop"}],
}


EXTRA_VALUES = st.one_of(
    st.integers(-(10**6), 10**6),
    st.floats(allow_nan=True, allow_infinity=True),
    st.decimals(allow_nan=True, allow_infinity=True),
    st.sampled_from([Decimal("sNaN"), Decimal("NaN"), Decimal("-Infinity"), True, False]),
    st.text(max_size=3),
    st.lists(st.integers(), max_size=2),
    st.none(),
)


@given(extras=st.dictionaries(st.text(max_size=4), EXTRA_VALUES, max_size=6))
def test_numeric_levels(extras: dict) -> None:
    """PBT scoring.numeric_levels: a result's levels are exactly its finite int,
    float and Decimal extras (no flag, text, list or non-finite value, a signalling NaN
    included), and reading them never raises. Harm: one odd extra aborts the nightly scan, or a
    NaN level reaches a gate."""
    levels = numeric_levels(extras)
    for key, value in extras.items():
        finite = (
            not isinstance(value, bool)
            and isinstance(value, (int, float, Decimal))
            and (value.is_finite() if isinstance(value, Decimal) else math.isfinite(value))
        )
        assert (key in levels) == finite
        if finite:
            assert levels[key] == float(value)
