"""PBT sizing.*: the position-size formula on the real
`size_position` and `evaluate_signal`. Money is Decimal; floats are refused."""

from decimal import Decimal

import pytest
from hat_risk_support import (
    D,
    cash_amounts,
    entry_and_stop,
    equities,
    fractions_open,
    volumes,
)
from hypothesis import assume, given
from hypothesis import strategies as st

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
    """Q is the largest whole number that respects all four terms, compared exactly: it fits
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
    bad = SizingInputs(**{**inp.__dict__, field: value})
    with pytest.raises(MissingInputError):
        size_position(bad)


@given(
    sizing_inputs(),
    st.sampled_from(
        ("allocation_risk_pct", "level_risk_mult", "p_max", "adv20", "free_cash", "equity")
    ),
    st.decimals(min_value=D("0"), max_value=D(1), places=2),
)
def test_monotone(inp: SizingInputs, field: str, shrink: Decimal) -> None:
    """Q never grows when r, the ladder multiplier, p_max, ADV20, C or E decreases."""
    value = getattr(inp, field)
    assume(value.is_finite() and value > 0)
    smaller = value * shrink
    assume(smaller > 0)
    tighter = SizingInputs(**{**inp.__dict__, field: smaller})
    assert size_position(tighter).quantity <= size_position(inp).quantity


@given(sizing_inputs(), st.decimals(min_value=D("0.01"), max_value=D("100"), places=2))
def test_wider_stop_never_grows(inp: SizingInputs, extra: Decimal) -> None:
    """Q never grows when the stop moves further from the entry."""
    wider = inp.stop_price - extra
    assume(wider > 0)
    assert (
        size_position(SizingInputs(**{**inp.__dict__, "stop_price": wider})).quantity
        <= size_position(inp).quantity
    )


@given(sizing_inputs())
def test_domain_errors_are_not_silent(inp: SizingInputs) -> None:
    """Out-of-domain numbers raise instead of sizing: a negative equity, a fraction above 1."""
    negative = SizingInputs(**{**inp.__dict__, "equity": -inp.equity})
    over_one = SizingInputs(**{**inp.__dict__, "p_max": D("1.5")})
    with pytest.raises(SizingInputError):
        size_position(negative)
    with pytest.raises(SizingInputError):
        size_position(over_one)
