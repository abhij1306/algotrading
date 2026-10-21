"""PBT sizing.* (HAT-RISK-010/020/021/022, HAT-DEC-B13): the position-size formula on the real
`size_position` and `RiskManager.evaluate_signal`. Money is Decimal; floats are refused."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from hat_risk_support import (
    AT,
    S1,
    S2,
    D,
    cash_amounts,
    decide,
    entry_and_stop,
    equities,
    fractions_open,
    levels,
    money,
    policy_with,
    valid_ctx,
    version_of,
    volumes,
)
from hypothesis import assume, given
from hypothesis import strategies as st

from app.hat.risk.decision import Reason
from app.hat.risk.exposure import LadderState
from app.hat.risk.policy import PolicyVersion, seed_policy
from app.hat.risk.sizing import (
    UNBOUNDED_CASH,
    MissingInputError,
    SizingInputError,
    SizingInputs,
    size_position,
)

cash_or_unbounded = st.one_of(cash_amounts, st.just(UNBOUNDED_CASH))


@st.composite
def sizing_inputs(draw: st.DrawFn) -> SizingInputs:
    entry, stop = draw(entry_and_stop())
    return SizingInputs(
        equity=draw(equities),
        allocation_risk_pct=draw(fractions_open),
        level_risk_mult=draw(st.sampled_from((D("0.5"), D(1)))),
        entry_price=entry,
        stop_price=stop,
        p_max=draw(fractions_open),
        adv20=draw(volumes),
        adv_cap_pct=draw(fractions_open),
        free_cash=draw(cash_or_unbounded),
    )


@given(sizing_inputs())
def test_formula_exact(inp: SizingInputs) -> None:
    """q is the largest whole number that respects all four terms, compared exactly: it fits
    every term, and one more share would break at least one."""
    sized = size_position(inp)
    q = sized.quantity
    r = inp.allocation_risk_pct * inp.level_risk_mult
    distance = inp.entry_price - inp.stop_price

    def fits(n: int) -> bool:
        return (
            n * distance <= inp.equity * r
            and n * inp.entry_price <= inp.equity * inp.p_max
            and n <= inp.adv_cap_pct * inp.adv20
            and n * inp.entry_price <= inp.free_cash
        )

    assert fits(q)
    assert not fits(q + 1)
    assert sized.risk_pct_used == r


@given(sizing_inputs(), st.sampled_from(("equity", "entry_price", "p_max", "adv20", "free_cash")))
def test_floats_rejected(inp: SizingInputs, field: str) -> None:
    """No float is used: any money or fraction passed as a float is refused, never coerced."""
    value = float(getattr(inp, field)) if getattr(inp, field).is_finite() else 1.0
    with pytest.raises(MissingInputError):
        size_position(SizingInputs(**{**inp.__dict__, field: value}))


@st.composite
def ctx_parts(draw: st.DrawFn) -> dict:
    entry, stop = draw(entry_and_stop())
    return {
        "strategy_key": draw(st.sampled_from((S1, S2))),
        "equity": draw(equities),
        "allocation_risk_pct": draw(
            st.decimals(min_value=D("0.0001"), max_value=D("0.0025"), places=4)
        ),
        "entry_price": entry,
        "stop_price": stop,
        "trigger_level": entry,
        "ltp": entry,
        "adv20": draw(volumes),
        "free_cash": draw(cash_amounts),
        "ladder": LadderState(draw(levels)),
        "total_equity": D(10**12),  # far above any open risk: the global cap cannot bind here
    }


@given(ctx_parts())
def test_risk_bounded(parts: dict) -> None:
    """For every accepted entry, q*(entry-stop) <= E*r, q*entry <= E*p_max, q <= adv_cap*ADV20
    and q*entry <= C, where r is the allocation risk times the ladder multiplier."""
    ctx = valid_ctx(**parts)
    decision = decide(ctx)
    policy = seed_policy()
    if not decision.accepted:
        return
    q = decision.quantity
    level_mult = policy.ladder[ctx.ladder.level].risk_mult
    assert (
        q * (ctx.entry_price - ctx.stop_price) <= ctx.equity * ctx.allocation_risk_pct * level_mult
    )
    assert q * ctx.entry_price <= ctx.equity * policy.p_max[ctx.strategy_key]
    assert q <= policy.adv_cap_pct * ctx.adv20
    assert q * ctx.entry_price <= ctx.free_cash
    assert decision.risk_amount == (ctx.entry_price - ctx.stop_price) * q
    assert decision.stop == ctx.stop_price


@given(
    sizing_inputs(),
    st.sampled_from(
        ("allocation_risk_pct", "level_risk_mult", "p_max", "adv20", "free_cash", "equity")
    ),
    st.decimals(min_value=D("0"), max_value=D(1), places=2),
)
def test_monotone(inp: SizingInputs, field: str, shrink: Decimal) -> None:
    """q never grows when r, the ladder multiplier, p_max, ADV20, C or E decreases."""
    value = getattr(inp, field)
    assume(value.is_finite() and value > 0)
    smaller = value * shrink
    assume(smaller > 0)
    tighter = SizingInputs(**{**inp.__dict__, field: smaller})
    assert size_position(tighter).quantity <= size_position(inp).quantity


@given(sizing_inputs(), st.decimals(min_value=D("0.01"), max_value=D("100"), places=2))
def test_wider_stop_never_grows(inp: SizingInputs, extra: Decimal) -> None:
    """q never grows when the stop moves further from the entry."""
    wider = inp.stop_price - extra
    assume(wider > 0)
    assert (
        size_position(SizingInputs(**{**inp.__dict__, "stop_price": wider})).quantity
        <= size_position(inp).quantity
    )


@given(ctx_parts())
def test_zero_skips_with_reason(parts: dict) -> None:
    """A zero quantity is a REJECT naming the binding term, with no quantity; a positive one is
    an ACCEPT whose binding term's floor equals the quantity."""
    ctx = valid_ctx(**parts)
    policy = seed_policy()
    level_mult = policy.ladder[ctx.ladder.level].risk_mult
    sized = size_position(
        SizingInputs(
            equity=ctx.equity,
            allocation_risk_pct=ctx.allocation_risk_pct,
            level_risk_mult=level_mult,
            entry_price=ctx.entry_price,
            stop_price=ctx.stop_price,
            p_max=policy.p_max[ctx.strategy_key],
            adv20=ctx.adv20,
            adv_cap_pct=policy.adv_cap_pct,
            free_cash=ctx.free_cash,
        )
    )
    decision = decide(ctx)
    if sized.quantity == 0:
        assert not decision.accepted
        assert decision.reason == f"size_zero_{sized.binding}"
        assert sized.terms[sized.binding] == 0
        assert decision.quantity is None
    else:
        # the base context cannot hit a cap, and the allocation risk is within the policy
        assert decision.accepted, decision.reason
        assert decision.quantity == sized.quantity
        assert sized.terms[decision.binding] == sized.quantity


@given(
    ctx_parts(),
    st.one_of(
        st.decimals(min_value=D("0"), max_value=D("5"), places=2).map(lambda x: ("equal", x)),
        st.decimals(min_value=D("0.01"), max_value=D("5"), places=2).map(lambda x: ("above", x)),
        st.just(("zero", D(0))),
    ),
)
def test_stop_below_entry_required(parts: dict, shape: tuple[str, Decimal]) -> None:
    """A stop at or above the entry, or not above zero, is a REJECT with no quantity."""
    kind, delta = shape
    entry = parts["entry_price"]
    stop = {"equal": entry, "above": entry + delta, "zero": D(0)}[kind]
    ctx = valid_ctx(**{**parts, "stop_price": stop})
    decision = decide(ctx)
    assert not decision.accepted
    assert decision.quantity is None
    assert decision.reason == Reason.STOP_NOT_BELOW_ENTRY
    assert (
        size_position(
            SizingInputs(
                equity=D(1000),
                allocation_risk_pct=D("0.01"),
                level_risk_mult=D(1),
                entry_price=entry,
                stop_price=stop,
                p_max=D("0.2"),
                adv20=D(1000),
                adv_cap_pct=D("0.01"),
                free_cash=D(10**6),
            )
        ).quantity
        == 0
    )


@given(
    st.decimals(min_value=D("0.01"), max_value=D(1), places=2),
    st.integers(min_value=100, max_value=1_000_000),
)
def test_adv_cap_from_policy(adv_cap: Decimal, adv20: int) -> None:
    """With every other term out of reach, the size is exactly floor(adv_cap_pct x ADV20) of the
    effective policy, not of a constant."""
    policy = policy_with(adv_cap_pct=str(adv_cap))
    decision = decide(
        valid_ctx(
            adv20=D(adv20),
            equity=D(10**12),
            total_equity=D(10**13),
            free_cash=D(10**15),
            policy_versions=[version_of(policy)],
        )
    )
    expected = int((adv_cap * adv20) // 1)
    if expected >= 1:
        assert decision.accepted, decision.reason
        assert decision.quantity == expected
        assert decision.binding == "liquidity"
    else:
        assert decision.reason == "size_zero_liquidity"


@given(
    ctx_parts(),
    st.decimals(min_value=D("0.01"), max_value=D(5), places=2),
    st.decimals(min_value=D("0.01"), max_value=D(5), places=2),
)
def test_multipliers_applied_once(parts: dict, regime: Decimal, health: Decimal) -> None:
    """The allocation's risk_pct already holds the regime and health multipliers: the decision's
    risk is allocation x ladder multiplier, whatever regime_mult and health_mult say."""
    base = decide(valid_ctx(**parts))
    other = decide(valid_ctx(**parts, regime_mult=regime, health_mult=health))
    assert (other.accepted, other.reason, other.quantity) == (
        base.accepted,
        base.reason,
        base.quantity,
    )
    if other.accepted:
        ladder = seed_policy().ladder[parts["ladder"].level]
        assert other.risk_pct_used == parts["allocation_risk_pct"] * ladder.risk_mult


@given(ctx_parts(), st.decimals(min_value=D("1.01"), max_value=D(10), places=2))
def test_allocation_above_policy_rejected(parts: dict, factor: Decimal) -> None:
    """An allocation risk above what the policy allows with multipliers at their bound is refused,
    so a multiplier cannot lift risk past the policy."""
    too_much = D("0.0025") * factor
    decision = decide(valid_ctx(**{**parts, "allocation_risk_pct": too_much}))
    assert not decision.accepted
    assert decision.reason == Reason.ALLOCATION_EXCEEDS_POLICY


version_times = st.integers(min_value=-30, max_value=30)


@given(
    ctx_parts(),
    st.lists(
        st.tuples(
            version_times,
            st.one_of(st.none(), version_times),
            st.decimals(min_value=D("0.01"), max_value=D("0.50"), places=2),
        ),
        min_size=1,
        max_size=5,
    ),
)
def test_uses_effective_policy(parts: dict, specs: list) -> None:
    """The decision depends only on the version with the latest effective_from at or before the
    signal that is not superseded; a version still in its cooling-off never applies."""
    versions = []
    for index, (start_days, end_days, cap) in enumerate(specs):
        starts = AT + timedelta(days=start_days, minutes=index)
        ends = None if end_days is None else AT + timedelta(days=end_days)
        policy = policy_with(adv_cap_pct=str(cap))
        versions.append(PolicyVersion(uuid.uuid4(), index + 1, starts, ends, policy))
    in_force = [
        v
        for v in versions
        if v.effective_from <= AT and (v.superseded_at is None or v.superseded_at > AT)
    ]
    ctx = valid_ctx(**parts, policy_versions=versions)
    decision = decide(ctx)
    if not in_force:
        assert decision.reason == Reason.MISSING_INPUT
        return
    chosen = max(in_force, key=lambda v: (v.effective_from, v.version))
    alone = decide(valid_ctx(**parts, policy_versions=[chosen]))
    assert decision.policy_version_id == chosen.version_id
    assert (decision.accepted, decision.reason, decision.quantity) == (
        alone.accepted,
        alone.reason,
        alone.quantity,
    )


@given(ctx_parts(), money("0.01", "0.50"))
def test_pending_increase_never_applies(parts: dict, bigger_cap: Decimal) -> None:
    """A version effective after the signal changes nothing about its decision."""
    pending = PolicyVersion(
        uuid.uuid4(), 2, AT + timedelta(days=7), None, policy_with(adv_cap_pct=str(bigger_cap))
    )
    now_only = valid_ctx(**parts)
    with_pending = valid_ctx(**parts, policy_versions=[*now_only.policy_versions, pending])
    a, b = decide(now_only), decide(with_pending)
    assert (a.accepted, a.reason, a.quantity, a.policy_version_id) == (
        b.accepted,
        b.reason,
        b.quantity,
        b.policy_version_id,
    )


@given(sizing_inputs())
def test_domain_errors_are_not_silent(inp: SizingInputs) -> None:
    """Out-of-domain numbers raise instead of sizing: a negative equity, a fraction above 1."""
    with pytest.raises(SizingInputError):
        size_position(SizingInputs(**{**inp.__dict__, "equity": -inp.equity}))
    with pytest.raises(SizingInputError):
        size_position(SizingInputs(**{**inp.__dict__, "p_max": D("1.5")}))
