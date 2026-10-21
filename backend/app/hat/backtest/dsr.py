"""The deflated Sharpe ratio (09 §4, HAT-BT-042; Bailey & López de Prado, 2014).

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
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist

from .metrics import VOLATILITY_FLOOR

__all__ = [
    "EULER_GAMMA",
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
