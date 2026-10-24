"""The deflated Sharpe ratio (HAT-BT-042; Bailey & López de Prado, 2014).

    DSR = Φ( (SR − SR0) · sqrt(T − 1) / sqrt(1 − γ3·SR + ((γ4 − 1) / 4)·SR²) )
    SR0 = sqrt(V) · ( (1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)) )

SR is the per-period Sharpe ratio of the run's returns (not annualised), T the number of returns,
γ3 their skewness and γ4 their (non-excess) kurtosis, N the family's trial count, V the variance
of the family's trial Sharpe ratios and γ the Euler-Mascheroni constant. With N = 1 or V = 0
there is nothing to deflate by: SR0 = 0 and DSR is the probabilistic Sharpe ratio against zero
([OWNER-PENDING] convention). A series the formula cannot score (fewer than 2 returns, no
variance, a denominator that is not positive (below `RADICAND_FLOOR`), a value that is not
finite) scores 0 with reason
`dsr_undefined`, never NaN, so a gate reading it fails closed.

The moments are the plain (population) sample moments, as in the paper's worked example.

A family's DSR is read through floors (PR 66, N15, `family_deflated_sharpe`): the trial count is
at least `MIN_TRIALS` and the variance at least `VARIANCE_FLOOR`, so the first run of a new
strategy (N = 1) or a family of near-identical variants (V near 0) is still deflated rather than
scored against a benchmark of zero. Both floors only ever raise SR0, so a floored DSR is never
above the paper's.

`pbo` is the probability of backtest overfitting by combinatorially symmetric cross-validation
(CSCV; Bailey, Borwein, López de Prado & Zhu, 2017) over a family's variants: the share of the
splits of the sessions into halves on which the variant best in sample ranks at or below the
median out of sample.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations
from statistics import NormalDist

from .metrics import VOLATILITY_FLOOR

__all__ = [
    "EULER_GAMMA",
    "MIN_TRIALS",
    "PBO_BLOCKS",
    "VARIANCE_FLOOR",
    "Pbo",
    "family_deflated_sharpe",
    "pbo",
    "RADICAND_FLOOR",
    "UNDEFINED",
    "Deflated",
    "ReturnStats",
    "deflated_sharpe",
    "expected_max_sharpe",
    "return_stats",
    "trial_variance",
]

EULER_GAMMA = 0.5772156649015329
UNDEFINED = "dsr_undefined"
# A denominator this close to zero is zero in floating point: 1 - g3 SR rounds to a few ulps
# where it should vanish, and dividing by it would score a degenerate series 1. It fails closed.
RADICAND_FLOOR = 1e-12
# [A] the floors of a family's DSR (PR 66, N15; confirmed, QA7): at least 5 trials ("N of 2-5"
# is where V is noise), and a trial-Sharpe variance of at least that of an annualised Sharpe
# spread of 0.5 (0.5 / sqrt(252) per session, squared).
MIN_TRIALS = 5
VARIANCE_FLOOR = 0.25 / 252
# [A] CSCV splits the sessions into this many blocks (an even number; 8 gives 70 splits).
PBO_BLOCKS = 8
_NORMAL = NormalDist()


@dataclass(frozen=True)
class ReturnStats:
    """What the DSR reads of one run's per-period returns."""

    sr: float  # mean / standard deviation, per period
    t: int
    skew: float
    kurtosis: float  # non-excess: 3 for a normal distribution


@dataclass(frozen=True)
class Deflated:
    prob: float  # in [0, 1]
    sr0: float
    reason: str | None = None  # UNDEFINED when the formula could not score the series


def return_stats(returns: Sequence[float]) -> ReturnStats | None:
    """The per-period Sharpe ratio, count, skewness and kurtosis of `returns`; None with fewer
    than 2 returns, a value that is not finite, or no variance."""
    t = len(returns)
    if t < 2 or not all(math.isfinite(r) for r in returns):
        return None
    mean = math.fsum(returns) / t
    m2 = math.fsum((r - mean) ** 2 for r in returns) / t
    if not math.isfinite(m2) or math.sqrt(m2) <= VOLATILITY_FLOOR:
        return None
    m3 = math.fsum((r - mean) ** 3 for r in returns) / t
    m4 = math.fsum((r - mean) ** 4 for r in returns) / t
    std = math.sqrt(m2)
    return ReturnStats(sr=mean / std, t=t, skew=m3 / std**3, kurtosis=m4 / m2**2)


def trial_variance(sharpes: Sequence[float]) -> float:
    """The sample variance of the family's trial Sharpe ratios; 0 with fewer than two."""
    values = [s for s in sharpes if math.isfinite(s)]
    if len(values) < 2:
        return 0.0
    mean = math.fsum(values) / len(values)
    return math.fsum((s - mean) ** 2 for s in values) / (len(values) - 1)


def expected_max_sharpe(trials: int, variance: float) -> float:
    """SR0: the Sharpe ratio the best of `trials` unskilled trials is expected to reach."""
    if isinstance(trials, bool) or not isinstance(trials, int) or trials < 1:
        raise ValueError("dsr_bad_trials")
    if not math.isfinite(variance) or variance < 0:
        raise ValueError("dsr_bad_variance")
    if trials == 1 or variance == 0:
        return 0.0
    first = _NORMAL.inv_cdf(1.0 - 1.0 / trials)
    second = _NORMAL.inv_cdf(1.0 - 1.0 / (trials * math.e))
    return math.sqrt(variance) * ((1.0 - EULER_GAMMA) * first + EULER_GAMMA * second)


def deflated_sharpe(stats: ReturnStats | None, trials: int, variance: float) -> Deflated:
    """The deflated Sharpe probability of a run against its family's `trials` and `variance`."""
    sr0 = expected_max_sharpe(trials, variance)
    if stats is None or stats.t < 2:
        return Deflated(0.0, sr0, UNDEFINED)
    sr = stats.sr
    radicand = 1.0 - stats.skew * sr + (stats.kurtosis - 1.0) / 4.0 * sr * sr
    if not math.isfinite(radicand) or radicand <= RADICAND_FLOOR:
        return Deflated(0.0, sr0, UNDEFINED)
    z = (sr - sr0) * math.sqrt(stats.t - 1) / math.sqrt(radicand)
    if not math.isfinite(z):
        return Deflated(0.0, sr0, UNDEFINED)
    prob = _NORMAL.cdf(z)
    return Deflated(min(1.0, max(0.0, prob)), sr0)


def family_deflated_sharpe(stats: ReturnStats | None, trials: int, variance: float) -> Deflated:
    """The DSR of a family's run: `deflated_sharpe` at a trial count of at least `MIN_TRIALS`
    and a variance of at least `VARIANCE_FLOOR` (a non-finite variance stays and is refused)."""
    floored = max(variance, VARIANCE_FLOOR) if math.isfinite(variance) else variance
    return deflated_sharpe(stats, max(trials, MIN_TRIALS), floored)


@dataclass(frozen=True)
class Pbo:
    prob: float  # in [0, 1]: the share of splits whose in-sample best is overfit out of sample
    splits: int  # C(blocks, blocks / 2)


def _sharpe(values: Sequence[float]) -> float:
    """Mean over population deviation; 0.0 without volatility."""
    mean = math.fsum(values) / len(values)
    std = math.sqrt(math.fsum((v - mean) ** 2 for v in values) / len(values))
    return mean / std if std > VOLATILITY_FLOOR else 0.0


def pbo(returns: Sequence[Sequence[float]], blocks: int = PBO_BLOCKS) -> Pbo | None:
    """The probability of backtest overfitting of `returns`, one equal-length per-session return
    series per variant (CSCV). The sessions are cut into `blocks` contiguous blocks of near-equal
    size; for each choice of half the blocks as in sample, the variant with the highest
    in-sample Sharpe ratio (the first on a tie) is ranked out of sample (1 the worst, ties
    averaged), and the split is overfit when its relative rank w = rank / (N + 1) is at most
    1/2 (logit w <= 0). None with fewer than 2 variants, unequal or non-finite series, an odd
    or non-positive block count, or fewer than 2 sessions per block."""
    n = len(returns)
    if n < 2 or isinstance(blocks, bool) or blocks < 2 or blocks % 2:
        return None
    t = len(returns[0])
    if t < 2 * blocks or any(len(r) != t for r in returns):
        return None
    if not all(math.isfinite(v) for r in returns for v in r):
        return None
    bounds = [t * k // blocks for k in range(blocks + 1)]
    overfit = 0
    splits = 0
    for chosen in combinations(range(blocks), blocks // 2):
        inside = set(chosen)
        is_rows = [i for k in inside for i in range(bounds[k], bounds[k + 1])]
        oos_rows = [
            i for k in range(blocks) if k not in inside for i in range(bounds[k], bounds[k + 1])
        ]
        is_sr = [_sharpe([r[i] for i in is_rows]) for r in returns]
        best = is_sr.index(max(is_sr))
        oos_sr = [_sharpe([r[i] for i in oos_rows]) for r in returns]
        mine = oos_sr[best]
        rank = (
            1 + sum(1 for s in oos_sr if s < mine) + (sum(1 for s in oos_sr if s == mine) - 1) / 2
        )
        overfit += rank / (n + 1) <= 0.5
        splits += 1
    return Pbo(overfit / splits, splits)
