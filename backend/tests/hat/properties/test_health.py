"""PBT health.bands: the health multiplier of a sleeve from its drawdown, on the
real `app.hat.engine.portfolio.health` and the policy's bands."""

from decimal import Decimal
from fractions import Fraction

import pytest
from hat_risk_support import policy_with
from hypothesis import given
from hypothesis import strategies as st

from app.hat.engine.portfolio.health import HealthError, drawdown, health_of
from app.hat.risk.policy import RiskPolicy, seed_policy

D = Decimal
MONEY = st.integers(1, 10_000_000).map(lambda n: D(n) / 100)
BANDS = st.sampled_from(((D("0.08"), D("0.12")), (D("0.05"), D("0.10")), (D("0.10"), D("0.20"))))


def policy_for(half: Decimal, zero: Decimal) -> RiskPolicy:
    doc = seed_policy().to_json()
    doc["health_bands"] = {**doc["health_bands"], "half_dd": str(half), "zero_dd": str(zero)}
    return policy_with(health_bands=doc["health_bands"])


@given(MONEY, MONEY, BANDS)
def test_bands(equity: Decimal, peak: Decimal, bands: tuple) -> None:
    """Under the first band the multiplier is the full value, from the first to the second band
    it is the half value, from the second on it is 0 and the sleeve goes to Probation; the
    comparisons are exact at the band edges."""
    half, zero = bands
    policy = policy_for(half, zero)
    got = health_of(equity, peak, policy)
    dd = 1 - Fraction(equity) / Fraction(peak)
    if dd >= Fraction(zero):
        assert (got.multiplier, got.probation) == (D(0), True)
    elif dd >= Fraction(half):
        assert (got.multiplier, got.probation) == (policy.health_half_mult, False)
    else:
        assert (got.multiplier, got.probation) == (policy.health_full_mult, False)
    assert drawdown(equity, peak) == 1 - equity / peak


@given(MONEY, MONEY, MONEY, BANDS)
def test_bands_are_monotone(peak: Decimal, a: Decimal, b: Decimal, bands: tuple) -> None:
    """A deeper drawdown never gives a larger multiplier, and the multiplier is never above the
    full value."""
    policy = policy_for(*bands)
    shallow, deep = max(a, b), min(a, b)
    assert health_of(deep, peak, policy).multiplier <= health_of(shallow, peak, policy).multiplier
    assert health_of(deep, peak, policy).multiplier <= policy.health_full_mult


@given(MONEY, st.sampled_from(("0.08", "0.12")))
def test_bands_edges(peak: Decimal, edge: str) -> None:
    """Equity exactly at a band's drawdown counts as in that band (the shortfall equals it)."""
    policy = seed_policy()
    at_edge = peak - D(edge) * peak
    got = health_of(at_edge, peak, policy)
    assert got.probation == (edge == "0.12")
    assert got.multiplier == (D(0) if edge == "0.12" else policy.health_half_mult)


@given(
    st.one_of(st.floats(allow_nan=True), st.integers(), st.none(), st.just("1")),
    MONEY,
)
def test_bands_refuse_unreadable_input(bad: object, good: Decimal) -> None:
    """Non-Decimal equity or peak, a peak at or below 0, negative equity and non-finite values
    fail loudly instead of reading as healthy."""
    policy = seed_policy()
    with pytest.raises(HealthError):
        health_of(bad, good, policy)  # type: ignore[arg-type]
    with pytest.raises(HealthError):
        health_of(good, bad, policy)  # type: ignore[arg-type]
    for pair in ((D("NaN"), good), (good, D(0)), (D(-1), good), (good, D("Infinity"))):
        with pytest.raises(HealthError):
            health_of(*pair, policy)


@given(MONEY, st.integers(0, 2500), BANDS)
def test_bands_across_the_drawdown_range(peak: Decimal, basis_points: int, bands: tuple) -> None:
    """Over drawdowns from 0 to 25% of the peak (the range that holds both band edges) the
    multiplier is the band's value and the Probation flag is set exactly from the second band."""
    half, zero = bands
    policy = policy_for(half, zero)
    equity = peak * (10_000 - basis_points) / 10_000
    got = health_of(equity, peak, policy)
    dd = D(basis_points) / 10_000
    expected = (
        D(0) if dd >= zero else policy.health_half_mult if dd >= half else policy.health_full_mult
    )
    assert got.multiplier == expected
    assert got.probation == (dd >= zero)
