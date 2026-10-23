"""The health multiplier of a sleeve from its drawdown (HAT-PORT-040), as pure functions.

dd = 1 - equity / equity_peak. Under the first band the multiplier is the policy's full value
(1.0), from the first band up to the second it is the half value (0.5), and at the second band
or beyond it is 0 and the sleeve moves to Probation. Bands and values are the policy's
(`health_bands`); the comparisons are exact (cross-multiplied), never a float division.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.hat.errors import HatError
from app.hat.risk.policy import RiskPolicy


class HealthError(HatError):
    """The equity pair cannot be read; the allocation fails instead of guessing."""


@dataclass(frozen=True)
class Health:
    multiplier: Decimal
    probation: bool  # the multiplier is 0: the sleeve moves to Probation (HAT-PORT-063)


def _readable(equity: object, peak: object) -> tuple[Decimal, Decimal]:
    if not (isinstance(equity, Decimal) and isinstance(peak, Decimal)):
        raise HealthError("health_not_exact")
    if not (equity.is_finite() and peak.is_finite()) or peak <= 0 or equity < 0:
        raise HealthError("health_unreadable")
    return equity, peak


def drawdown(equity: Decimal, peak: Decimal) -> Decimal:
    """1 - equity / peak for a readable pair (a value under 0 when equity is above the peak)."""
    eq, top = _readable(equity, peak)
    return 1 - eq / top


def health_of(equity: Decimal, peak: Decimal, policy: RiskPolicy) -> Health:
    """The health multiplier of (equity, equity_peak) under `policy`'s bands.

    Raises `HealthError` for equity below 0, a peak at or below 0, or a non-Decimal value.
    """
    eq, top = _readable(equity, peak)
    shortfall = top - eq
    if shortfall >= policy.health_zero_dd * top:
        return Health(Decimal(0), True)
    if shortfall >= policy.health_half_dd * top:
        # never above the full value, so the multiplier cannot rise as the drawdown deepens
        return Health(min(policy.health_half_mult, policy.health_full_mult), False)
    return Health(policy.health_full_mult, False)
