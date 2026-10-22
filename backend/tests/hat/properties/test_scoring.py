"""PBT scoring.* (HAT-CRIT-010, 022, 030, 031, 032, 123): how criteria combine into a stage result.

The criteria are real: setup-stage `rs_rank` instances over a one-row universe whose `rs_pct` is
50. A hard instance passes when `min_percentile <= 50`; a score instance scores
`(50 - score_from) / (score_full - score_from)`. Oracles are written in `Fraction`s apart from the
code under test.
"""

import math
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Literal

import pytest
from hat.hat_criteria_support import Lcg, frame, make_rows, universe_frame
from hypothesis import given
from hypothesis import strategies as st

from app.hat.engine.criteria import REGISTRY, CriterionResult, CriterionType, EvalContext
from app.hat.engine.criteria.base import ParamModel
from app.hat.engine.scan import criteria_results
from app.hat.engine.strategy.config import Instance, parse_config
from app.hat.engine.strategy.scoring import (
    EMPTY_SCORE,
    PlaybookEvaluation,
    evaluate_playbook,
    evaluate_stage,
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


def instance_score(instance: Instance) -> Fraction:
    low, full = Fraction(instance.params["score_from"]), Fraction(instance.params["score_full"])
    return min(Fraction(1), max(Fraction(0), (Fraction(RS) - low) / (full - low)))


def hard_passes(instance: Instance) -> bool:
    return RS >= instance.params["min_percentile"]


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


@given(items=instances(), threshold=st.sampled_from([0.0, 0.3, 0.6, 1.0]))
def test_empty_score_is_one(items: list[Instance], threshold: float) -> None:
    """Without an enabled score criterion the score is 1.0 and only the hard criteria decide."""
    hard_only = [i for i in items if i.mode == "hard" or not i.enabled]
    outcome = evaluate_stage("setup", hard_only, ctx(), threshold)
    active = [i for i in hard_only if i.enabled]
    if all(hard_passes(i) for i in active):
        assert outcome.passed
        assert outcome.score == 1.0
    else:
        assert not outcome.passed
        assert outcome.failed is not None


@given(hard_ok=st.booleans(), score=st.floats(0, 1), threshold=st.floats(0, 1))
def test_threshold_gate(hard_ok: bool, score: float, threshold: float) -> None:
    """A symbol passes iff every hard criterion passes and the score reaches the threshold."""
    assert passes_threshold(hard_ok, score, threshold) == (hard_ok and score >= threshold)
    assert passes_threshold(True, threshold, threshold)  # exactly at the bar passes


@given(items=instances(), threshold=st.sampled_from([0.0, 0.3, 0.6, 0.99, 1.0]))
def test_stage_result_follows_the_formula(items: list[Instance], threshold: float) -> None:
    """On real criteria the stage result is: all enabled hard criteria pass and the weighted mean
    of the enabled score criteria (1.0 if none) is at least the threshold."""
    outcome = evaluate_stage("setup", items, ctx(), threshold)
    active = [i for i in items if i.enabled]
    scored = [(Fraction(i.weight), instance_score(i)) for i in active if i.mode == "score"]
    hard_ok = all(hard_passes(i) for i in active if i.mode == "hard")
    if not hard_ok:
        assert not outcome.passed
        return
    weights = sum(w for w, _ in scored)
    want = sum(w * s for w, s in scored) / weights if scored else Fraction(1)
    assert outcome.score == pytest.approx(float(want), abs=1e-12)
    assert outcome.passed == (outcome.score >= threshold)


@given(items=instances(), threshold=st.sampled_from([0.0, 0.6]))
def test_disabled_is_noop(items: list[Instance], threshold: float) -> None:
    """A disabled criterion is skipped: the result equals the result with it removed."""
    kept = [i for i in items if i.enabled]
    assert evaluate_stage("setup", items, ctx(), threshold) == evaluate_stage(
        "setup", kept, ctx(), threshold
    )


@given(items=instances())
def test_hard_short_circuit(items: list[Instance]) -> None:
    """Evaluation stops at the first failing hard criterion by `order`, names it with its reason
    and value, and evaluates nothing after it. When several hard criteria fail, the stored one
    is the lowest `order` whatever the list order."""
    outcome = evaluate_stage("setup", items, ctx())
    active = sorted((i for i in items if i.enabled), key=lambda i: i.order)
    failing = [i for i in active if i.mode == "hard" and not hard_passes(i)]
    if not failing:
        assert outcome.failed is None
        assert [o.order for o in outcome.outcomes] == [i.order for i in active]
        return
    first = failing[0]
    assert outcome.failed is not None
    assert not outcome.passed
    assert outcome.failed.order == first.order == outcome.outcomes[-1].order
    assert outcome.reason == outcome.failed.result.reason == "rs_below_min"
    assert outcome.failed.result.value == RS
    assert [o.order for o in outcome.outcomes] == [
        i.order for i in active if i.order <= first.order
    ]


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


@given(
    series=st.sampled_from(["EQ", "SM"]),
    regime=st.booleans(),
    rs=st.sampled_from([0.0, 80.0, 99.0]),
)
def test_stage_order(series: str, regime: bool, rs: float) -> None:
    """Stages are evaluated universe, regime gate, setup, and a symbol reaches a later stage only
    after passing every earlier one; the recorded stages stop at the first failure."""
    config = parse_config(CONFIG)
    result = evaluate_playbook(config, "default", ctx(series, rs), regime_allows_longs=regime)
    names = [s.stage for s in result.stages]
    assert names == ["universe", "regime", "setup"][: len(names)]
    assert all(s.passed for s in result.stages[:-1])
    if series != "EQ":
        assert names == ["universe"]
        assert not result.passed
    elif not regime:
        assert names == ["universe", "regime"]
        assert not result.passed
    else:
        assert names == ["universe", "regime", "setup"]
        assert result.passed == result.stages[-1].passed
        assert result.passed == (
            evaluate_stage("setup", config.playbooks[0].setup.criteria, ctx(series, rs), 0.5).passed
        )
    assert result.stopped_at == (None if result.passed else names[-1])


class ProbeParams(ParamModel):
    """A probe criterion: reports `value` under `key` (none when None) and passes when `ok`."""

    key: Literal["pivot", "consolidation_high", "final_contraction_low"]
    value: float | None
    ok: bool


@given(
    probes=st.lists(
        st.tuples(
            st.sampled_from(["pivot", "consolidation_high", "final_contraction_low"]),
            st.one_of(st.none(), st.integers(1, 50).map(float)),
            st.booleans(),
            st.sampled_from(["hard", "score"]),
        ),
        max_size=6,
    ),
    shuffle=st.randoms(use_true_random=False),
)
def test_stage_levels(probes: list, shuffle) -> None:  # noqa: ANN001
    """PBT scoring.stage_levels (HAT-CRIT-123): each criterion sees, as `ctx.levels`, the
    levels the passed criteria before it reported, each key from the first to report it; the
    stage's `levels` and the levels the scan publishes are the same map; a failed result adds
    nothing, and the shared context is never changed. Harm: the gate approves one level while
    the watchlist row, and so the trigger, uses another."""
    seen: list[dict[str, float]] = []

    def probe(context: EvalContext, params: ProbeParams) -> CriterionResult:
        seen.append(dict(context.levels))
        extras = {} if params.value is None else {params.key: params.value}
        return CriterionResult(params.ok, 1.0 if params.ok else 0.0, None, "ok" if params.ok
                               else "probe_failed", extras)  # fmt: skip

    probe_type = CriterionType("level_probe", "setup", ProbeParams, (), probe)
    shared = ctx()
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(REGISTRY, "level_probe", probe_type)
        items = [
            Instance(type="level_probe", stage="setup", mode=mode, weight=1.0, order=order,
                     params={"key": key, "value": value, "ok": ok})
            for order, (key, value, ok, mode) in enumerate(probes)
        ]  # fmt: skip
        shuffle.shuffle(items)
        outcome = evaluate_stage("setup", items, shared)
    expected: dict[str, float] = {}
    for index, (key, value, ok, mode) in enumerate(probes[: len(seen)]):
        assert seen[index] == expected
        if ok and value is not None:
            expected.setdefault(key, value)
        if mode == "hard" and not ok:
            break
    assert dict(outcome.levels) == expected
    published = criteria_results(PlaybookEvaluation("p", outcome.passed, (outcome,)))["levels"]
    assert {k: float(v) for k, v in published.items()} == expected
    assert dict(shared.levels) == {}


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
    """PBT scoring.numeric_levels (HAT-CRIT-123): a result's levels are exactly its finite int,
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
