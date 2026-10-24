"""The deflated Sharpe ratio of `app.hat.backtest.dsr` against the formula written out with
`math.erf`, its family floors, and the probability of backtest overfitting."""

import math
import statistics
from itertools import combinations

from hat_support import memory_engine
from hypothesis import assume, example, given
from hypothesis import strategies as st

from app.hat.backtest.dsr import (
    MIN_TRIALS,
    UNDEFINED,
    VARIANCE_FLOOR,
    ReturnStats,
    deflated_sharpe,
    expected_max_sharpe,
    family_deflated_sharpe,
    pbo,
    return_stats,
)
from app.hat.schema import seed_hat_reference

ENGINE = memory_engine()
seed_hat_reference(ENGINE)
SR = st.integers(-500, 500).map(lambda n: n / 1000)  # per-period Sharpe ratios
SKEW = st.integers(-300, 300).map(lambda n: n / 100)
EXCESS = st.integers(0, 2000).map(lambda n: n / 100)
T = st.integers(2, 3000)
N = st.integers(1, 10_000)
V = st.integers(0, 1000).map(lambda n: n / 100_000)


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


@st.composite
def stats(draw: st.DrawFn, sr: st.SearchStrategy[float] = SR) -> ReturnStats:
    skew = draw(SKEW)
    # a distribution's kurtosis is at least 1 + skew^2
    return ReturnStats(sr=draw(sr), t=draw(T), skew=skew, kurtosis=1 + skew**2 + draw(EXCESS))


def _slope(s: ReturnStats, sr0: float) -> float:
    """The sign of d DSR / d SR at s.sr: 1 - g3 (SR + SR0) / 2 + (g4 - 1) / 4 SR SR0."""
    return 1 - s.skew * (s.sr + sr0) / 2 + (s.kurtosis - 1) / 4 * s.sr * sr0


# --- -------------------------------------------------------------------------------


@given(stats(), N, V, N)
@example(ReturnStats(0.1, 250, 0.0, 3.0), 1, 0.0, 2)
def test_bounds_and_monotonicity(s: ReturnStats, trials: int, variance: float, more: int) -> None:
    """DSR is in [0, 1] and never NaN, and it does not rise with the trial count."""
    got = deflated_sharpe(s, trials, variance)
    assert 0.0 <= got.prob <= 1.0
    assert not math.isnan(got.prob)
    assert deflated_sharpe(s, trials + more, variance).prob <= got.prob + 1e-12


@given(stats(), SR, N, V)
def test_bounds_and_monotonicity_in_sr(s: ReturnStats, other: float, trials: int, v: float) -> None:
    """With everything else fixed, DSR does not fall as SR rises wherever the formula's slope in
    SR is non-negative: 1 - g3 (SR + SR0) / 2 + (g4 - 1) / 4 SR SR0 >= 0 at both ends (the slope
    is linear in SR, so then on the whole interval). Outside that region a higher SR with a
    heavy skew can score lower; that is the formula, not the code (recorded)."""
    low, high = sorted((s.sr, other))
    a, b = ReturnStats(low, s.t, s.skew, s.kurtosis), ReturnStats(high, s.t, s.skew, s.kurtosis)
    sr0 = expected_max_sharpe(trials, v)
    assume(_slope(a, sr0) >= 0 and _slope(b, sr0) >= 0)
    first, second = deflated_sharpe(a, trials, v), deflated_sharpe(b, trials, v)
    assume(first.reason is None and second.reason is None)
    assert first.prob <= second.prob + 1e-12


@given(stats(), st.sampled_from(["one_trial", "no_variance"]), N, V)
def test_bounds_and_monotonicity_first_trial(
    s: ReturnStats, case: str, trials: int, v: float
) -> None:
    """With N = 1 or V = 0, SR0 = 0 and DSR is the probabilistic Sharpe ratio against zero."""
    trials, v = (1, v) if case == "one_trial" else (trials, 0.0)
    got = deflated_sharpe(s, trials, v)
    radicand = 1 - s.skew * s.sr + (s.kurtosis - 1) / 4 * s.sr**2
    assert got.sr0 == 0.0
    if radicand <= 0:
        assert (got.prob, got.reason) == (0.0, UNDEFINED)
    else:
        want = phi(s.sr * math.sqrt(s.t - 1) / math.sqrt(radicand))
        assert math.isclose(got.prob, want, rel_tol=1e-9, abs_tol=1e-12)


@given(
    st.one_of(
        st.lists(st.integers(-50, 50).map(lambda n: n / 1000), max_size=1),
        st.tuples(st.integers(-50, 50), st.integers(2, 30)).map(lambda p: [p[0] / 1000] * p[1]),
        st.lists(st.sampled_from([math.nan, math.inf, 0.01]), min_size=2, max_size=5).filter(
            lambda xs: not all(math.isfinite(x) for x in xs)
        ),
    ),
    N,
    V,
)
def test_bounds_and_monotonicity_undefined(returns: list[float], trials: int, v: float) -> None:
    """Fewer than 2 returns, no variance, or a value that is not finite scores 0 with reason
    `dsr_undefined`, never NaN; so does a non-positive denominator."""
    got = deflated_sharpe(return_stats(returns), trials, v)
    assert (got.prob, got.reason) == (0.0, UNDEFINED)


@given(st.integers(1, 300).map(lambda n: n / 100), st.integers(0, 500), T, N, V)
def test_bounds_and_monotonicity_bad_denominator(
    sr: float, extra: int, t: int, trials: int, v: float
) -> None:
    """A non-positive denominator 1 - g3 SR + (g4 - 1)/4 SR^2 (or one within floating-point
    noise of zero) scores 0, never NaN or 1. Real moments
    keep it a square (g4 >= 1 + g3^2 makes it >= (1 - g3 SR / 2)^2), so this takes moments
    below that bound: g4 = 1 and g3 >= 1 / SR."""
    s = ReturnStats(sr, t, 1 / sr + extra / 100, 1.0)
    got = deflated_sharpe(s, trials, v)
    assert (got.prob, got.reason) == (0.0, UNDEFINED)


# --- the family floors and PBO ------------------------------


@given(stats(), N, V)
@example(ReturnStats(0.1, 250, 0.0, 3.0), 1, 0.0)  # the first run of a new strategy
def test_family_floors(s: ReturnStats, trials: int, variance: float) -> None:
    """A family's DSR is the paper's at a trial count of at least `MIN_TRIALS` and a variance of
    at least `VARIANCE_FLOOR`: never above the unfloored DSR, and the first run of a new
    strategy (N = 1) or a family of identical variants (V = 0) is deflated against a positive
    SR0. Harm (N15): a new strategy's first lucky run is scored undeflated and promoted."""
    got = family_deflated_sharpe(s, trials, variance)
    floored = deflated_sharpe(s, max(trials, MIN_TRIALS), max(variance, VARIANCE_FLOOR))
    assert (got.prob, got.sr0, got.reason) == (floored.prob, floored.sr0, floored.reason)
    assert got.sr0 >= expected_max_sharpe(MIN_TRIALS, VARIANCE_FLOOR) > 0
    raw = deflated_sharpe(s, trials, variance)
    assert got.prob <= raw.prob + 1e-12


NOISE = st.lists(st.integers(-40, 40).map(lambda n: n / 1000), min_size=16, max_size=48)
BLOCKS = st.sampled_from([2, 4, 6, 8])


@given(NOISE, st.lists(st.integers(-20, 20), min_size=2, max_size=6, unique=True), BLOCKS)
@example([0.01, -0.01, 0.02, 0.0] * 4, [0, 1], 2)
def test_pbo_ranked_variants(noise: list[float], shifts: list[int], blocks: int) -> None:
    """Variants that differ only by a constant shift of the same returns keep their Sharpe order
    on every half of the sessions, so the in-sample best is the out-of-sample best and PBO is
    0; identical variants are never distinguishable, so PBO is 1 (no evidence of skill). Every
    PBO is in [0, 1] over C(S, S/2) splits. Harm: a family whose best variant is genuinely
    best reads as overfit, or one with nothing to choose between reads as skill."""
    assume(len(noise) >= 2 * blocks)
    cut = [len(noise) * k // blocks for k in range(blocks + 1)]
    # every block varies, so every half has a volatility and the shift orders the Sharpe ratios
    assume(all(len(set(noise[cut[k] : cut[k + 1]])) > 1 for k in range(blocks)))
    shifted = [[x + k / 10_000 for x in noise] for k in shifts]
    found = pbo(shifted, blocks)
    assert found is not None
    assert found.splits == math.comb(blocks, blocks // 2)
    assert found.prob == 0.0
    same = pbo([noise] * len(shifts), blocks)
    assert same is not None
    assert same.prob == 1.0


def _sr(values: list[float]) -> float:
    sd = statistics.pstdev(values)
    return statistics.fmean(values) / sd if sd > 1e-12 else 0.0


@given(NOISE, NOISE, BLOCKS)
@example([0.01, 0.02, 0.03, 0.04, -0.01, -0.02, -0.03, -0.04] * 2, [0.0, 0.01] * 8, 2)
def test_pbo_two_variants(a: list[float], b: list[float], blocks: int) -> None:
    """With two variants, PBO is the share of the splits of the sessions (half the S blocks in
    sample) on which the in-sample winner (the first on a tie) has an out-of-sample Sharpe
    ratio no higher than the other's, counted here split by split. Harm: an in-sample
    winner that loses out of sample is counted as robust."""
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    assume(n >= 2 * blocks)
    cut = [n * k // blocks for k in range(blocks + 1)]
    lost = 0
    halves = list(combinations(range(blocks), blocks // 2))
    for half in halves:
        ins = [i for k in half for i in range(cut[k], cut[k + 1])]
        out = [i for i in range(n) if i not in set(ins)]
        winner, other = (a, b) if _sr([a[i] for i in ins]) >= _sr([b[i] for i in ins]) else (b, a)
        lost += _sr([winner[i] for i in out]) <= _sr([other[i] for i in out])
    found = pbo([a, b], blocks)
    assert found is not None
    assert math.isclose(found.prob, lost / len(halves), abs_tol=1e-12)


@given(st.sampled_from(["one_variant", "odd_blocks", "unequal", "short", "not_finite"]), NOISE)
def test_pbo_refused(case: str, noise: list[float]) -> None:
    """PBO is not computed (None) for fewer than 2 variants, an odd block count, series of
    unequal length, fewer than 2 sessions per block or a value that is not finite. Harm: a
    one-variant family or a short history reads as a measured overfitting probability."""
    matrix, blocks = {
        "one_variant": ([noise], 2),
        "odd_blocks": ([noise, noise], 3),
        "unequal": ([noise, noise[:-1]], 2),
        "short": ([noise, noise], len(noise) // 2 + 2 + len(noise) % 2),
        "not_finite": ([noise, [*noise[:-1], math.inf]], 2),
    }[case]
    if case == "short" and blocks % 2:
        blocks += 1
    assert pbo(matrix, blocks) is None
