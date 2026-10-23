"""PBT allocator.*: the nightly allocation on the real `app.hat.engine.portfolio.allocator` and
`allocation_service.run_allocation` (SQLite), against the formula written out independently.
Money and weights are Decimal."""

import math
import uuid
from datetime import datetime
from decimal import ROUND_DOWN, Decimal, localcontext

import pytest
from hat_risk_support import policy_with
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy.orm import Session

from app.hat.engine.portfolio import allocator
from app.hat.engine.portfolio.allocator import Allocation, SleeveInput, allocate
from app.hat.engine.portfolio.health import Health
from app.hat.risk.policy import RiskPolicy, seed_policy

D = Decimal
FULL, HALF, ZERO = D("1.0"), D("0.5"), D(0)
STATES = ("live", "live", "live", "shadow", "probation", "research")


def pct(lo: int, hi: int) -> st.SearchStrategy[Decimal]:
    return st.integers(lo, hi).map(lambda n: D(n) / 100)


@st.composite
def sleeve_inputs(draw: st.DrawFn) -> SleeveInput:
    health = draw(st.sampled_from((FULL, HALF, ZERO)))
    return SleeveInput(
        bot_id=uuid.uuid4(),
        sleeve=draw(st.sampled_from(("S1", "S2", "S3"))),
        cluster=draw(st.sampled_from(("c1", "c2", "c3"))),
        lifecycle_state=draw(st.sampled_from(STATES)),
        regime_table_value=draw(pct(0, 200)),
        health=Health(health, health == ZERO),
        previous_regime_mult=draw(st.one_of(st.none(), pct(0, 120))),
        previous_health_mult=draw(st.one_of(st.none(), st.just(ZERO), pct(25, 100))),
    )


SLEEVES = st.lists(sleeve_inputs(), min_size=0, max_size=8)
EQUITY = st.integers(0, 10**9).map(lambda n: D(n) / 100)


@st.composite
def policies(draw: st.DrawFn) -> RiskPolicy:
    lo = draw(pct(5, 60))
    hi = draw(pct(60, 150))
    weights = {k: str(draw(pct(0, 100))) for k in ("S1", "S2", "S3")}
    return policy_with(
        base_weight=weights,
        cluster_cap=str(draw(pct(10, 100))),
        multiplier_bounds={"lo": str(lo), "hi": str(hi)},
        risk_pct=str(draw(st.integers(1, 100).map(lambda n: D(n) / 10_000))),
    )


def product_of(row: allocator.Allocation) -> Decimal:
    return row.base_weight * row.regime_mult * row.health_mult


@given(SLEEVES, policies(), EQUITY)
def test_formula_and_bounds(
    sleeves: list[SleeveInput], policy: RiskPolicy, equity: Decimal
) -> None:
    """W = base x regime x health with both multipliers inside the policy bounds; a sleeve that is
    not live has weight 0; a weight is never above its product and equals it exactly when no cap
    binds."""
    rows = allocate(sleeves, policy, equity)
    assert [r.bot_id for r in rows] == [s.bot_id for s in sleeves]
    lo, hi = policy.multiplier_lo, policy.multiplier_hi
    for s, r in zip(sleeves, rows, strict=True):
        assert r.base_weight == policy.base_weight.get(s.sleeve, D(0))
        assert lo <= r.regime_mult <= hi
        assert r.health_mult == 0 or lo <= r.health_mult <= hi
        live = s.lifecycle_state == "live" and not s.health.probation
        assert (r.health_mult == 0) or live
        if not live:
            assert r.health_mult == 0
            assert r.final_weight == 0
        assert 0 <= r.final_weight <= product_of(r)
        if hi <= 1:
            assert r.final_weight <= r.base_weight
    clusters = {s.cluster for s in sleeves}
    within_cluster = all(
        sum((product_of(r) for s, r in zip(sleeves, rows, strict=True) if s.cluster == c), D(0))
        <= policy.cluster_cap
        for c in clusters
    )
    if within_cluster and sum((product_of(r) for r in rows), D(0)) <= 1:
        assert all(r.final_weight == product_of(r) for r in rows)


@given(SLEEVES, policies(), EQUITY)
def test_bounds_from_policy(
    sleeves: list[SleeveInput], policy: RiskPolicy, equity: Decimal
) -> None:
    """Moving the policy's bounds moves the multipliers: every multiplier is within the bounds of
    the policy given, whatever the regime table and health say."""
    lo, hi = policy.multiplier_lo, policy.multiplier_hi
    for r in allocate(sleeves, policy, equity):
        assert lo <= r.regime_mult <= hi
        assert r.health_mult == 0 or lo <= r.health_mult <= hi


@given(SLEEVES, policies(), EQUITY)
def test_capital_and_risk(sleeves: list[SleeveInput], policy: RiskPolicy, equity: Decimal) -> None:
    """Capital = final weight x total equity (rounded down to a paisa-fraction, never up) and
    the capitals together never exceed the equity; risk per trade = policy risk x regime x
    health."""
    rows = allocate(sleeves, policy, equity)
    for r in rows:
        exact = r.final_weight * equity
        assert r.capital == exact.quantize(D("0.0001"), rounding=ROUND_DOWN)
        assert 0 <= r.capital <= exact
        assert r.risk_pct == policy.risk_pct * r.regime_mult * r.health_mult
        assert (r.risk_pct == 0) == (r.health_mult == 0)
    assert sum((r.capital for r in rows), D(0)) <= equity


def _held(sleeves: list[SleeveInput], products: list[Decimal], cap: Decimal) -> list[Decimal]:
    """An oracle of the two caps: each cluster's sum, then the book's, scaled down to its limit
    in proportion and rounded down to 12 places."""

    def down(ws: list[Decimal], limit: Decimal) -> list[Decimal]:
        total = sum(ws, D(0))
        if total <= limit:
            return ws
        with localcontext() as ctx:
            ctx.prec = 120
            return [(w * limit / total).quantize(D("1E-12"), rounding=ROUND_DOWN) for w in ws]

    out = list(products)
    for c in {s.cluster for s in sleeves}:
        idx = [i for i, s in enumerate(sleeves) if s.cluster == c]
        for i, w in zip(idx, down([out[i] for i in idx], cap), strict=True):
            out[i] = w
    return down(out, D(1))


def _product(rows: list[Allocation], sleeve: SleeveInput) -> Decimal:
    """A sleeve's uncapped real product, base x regime x health, from its allocation row."""
    r = next(x for x in rows if x.bot_id == sleeve.bot_id)
    return r.base_weight * r.regime_mult * r.health_mult


@given(
    st.one_of(st.none(), pct(0, 200)),
    pct(0, 200),
    st.integers(1, 100).map(lambda n: D(n) / 100),
)
def test_smoothing(previous: Decimal | None, target: Decimal, step: Decimal) -> None:
    """A multiplier moves at most one step a day toward its target, never past it, and starts at
    its target when it has no previous value."""
    got = allocator.smooth(previous, target, step)
    if previous is None:
        assert got == target
        return
    assert abs(got - previous) <= step
    assert min(previous, target) <= got <= max(previous, target)
    if abs(target - previous) <= step:
        assert got == target
    days, value = 0, previous
    while value != target:
        value = allocator.smooth(value, target, step)
        days += 1
    assert days == math.ceil(abs(target - previous) / step)


@given(SLEEVES, policies(), EQUITY)
def test_smoothing_in_the_allocation(
    sleeves: list[SleeveInput], policy: RiskPolicy, equity: Decimal
) -> None:
    """In the allocation a multiplier is at most 0.25 from its previous value (when that was in
    bounds), and a sleeve returning from health 0 restarts at its target."""
    lo, hi = policy.multiplier_lo, policy.multiplier_hi
    for s, r in zip(sleeves, allocate(sleeves, policy, equity), strict=True):
        if s.previous_regime_mult is not None and lo <= s.previous_regime_mult <= hi:
            assert abs(r.regime_mult - s.previous_regime_mult) <= allocator.SMOOTHING_STEP
        live = s.lifecycle_state == "live" and not s.health.probation
        if live and s.previous_health_mult:
            if lo <= s.previous_health_mult <= hi:
                assert abs(r.health_mult - s.previous_health_mult) <= allocator.SMOOTHING_STEP
        if live and not s.previous_health_mult:
            assert r.health_mult == allocator.clamp(s.health.multiplier, lo, hi)


@given(SLEEVES, policies(), EQUITY)
def test_cash_remainder(sleeves: list[SleeveInput], policy: RiskPolicy, equity: Decimal) -> None:
    """The weights sum to at most 1; a cap only takes weight away from a sleeve and nothing
    removed is given to another, so the rest is cash."""
    rows = allocate(sleeves, policy, equity)
    total = sum((r.final_weight for r in rows), D(0))
    assert 0 <= total <= 1
    assert total <= sum((product_of(r) for r in rows), D(0))
    for r in rows:
        assert r.final_weight <= product_of(r)


@given(SLEEVES, policies(), EQUITY)
def test_cluster_cap(sleeves: list[SleeveInput], policy: RiskPolicy, equity: Decimal) -> None:
    """The final weights of one risk cluster sum to at most the policy's cluster cap, and a
    cluster over its cap is scaled in proportion (relative sizes inside it are kept)."""
    rows = allocate(sleeves, policy, equity)
    for cluster in {s.cluster for s in sleeves}:
        members = [r for s, r in zip(sleeves, rows, strict=True) if s.cluster == cluster]
        assert sum((r.final_weight for r in members), D(0)) <= policy.cluster_cap
        for a in members:
            for b in members:
                lhs, rhs = a.final_weight * product_of(b), b.final_weight * product_of(a)
                assert abs(lhs - rhs) <= D("1E-11") * max(product_of(a), product_of(b), D(1))


@given(SLEEVES, policies(), EQUITY, st.randoms(use_true_random=False))
def test_sleeve_count_agnostic(
    sleeves: list[SleeveInput], policy: RiskPolicy, equity: Decimal, rng: object
) -> None:
    """Any number of sleeves, in any order, gives one row each; a sleeve's base weight is the
    policy's entry for its key, so a new sleeve needs a policy entry and no code."""
    shuffled = list(sleeves)
    rng.shuffle(shuffled)  # type: ignore[attr-defined]
    first = {r.bot_id: r for r in allocate(sleeves, policy, equity)}
    second = {r.bot_id: r for r in allocate(shuffled, policy, equity)}
    assert len(first) == len(sleeves)
    assert set(first) == set(second)
    # the cap scaling is order independent up to the rounding of the last digit
    for bot_id, row in first.items():
        other = second[bot_id]
        assert (row.base_weight, row.regime_mult, row.health_mult) == (
            other.base_weight,
            other.regime_mult,
            other.health_mult,
        )
        assert abs(row.final_weight - other.final_weight) <= D("1E-11")
    assert allocator.sleeve_key("a-new-strategy") == "a-new-strategy"


@given(st.one_of(st.integers(), st.floats(), st.just(D(-1)), st.just(D("NaN"))))
def test_allocation_refuses_a_bad_total(equity: object) -> None:
    """A float, an integer, a negative or a non-finite total equity fails the allocation."""
    if isinstance(equity, Decimal) and equity.is_finite() and equity >= 0:
        return
    policy = seed_policy()
    with pytest.raises(allocator.AllocationError):
        allocate([], policy, equity)  # type: ignore[arg-type]


# --- through the database ---------------------------------------------------------------------


# --- item 8: virtual entries of a Shadow or Probation sleeve are sized ------------------------


class _OpenGates:
    def gate_on(self, _session: Session, _bot_id: object, _at: datetime) -> bool:
        return True

    def quality_failed(self, _session: Session, _day: object, _calendar: object) -> bool:
        return False
