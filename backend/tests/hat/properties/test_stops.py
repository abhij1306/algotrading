"""PBT for stop pricing at the broker (HAT-EXEC-062)."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.hat.execution.gtt import GttPriceError, gtt_limit_price

ticks = st.sampled_from(
    [Decimal("0.01"), Decimal("0.05"), Decimal("0.1"), Decimal("1"), Decimal("5")]
)
triggers = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("500000"), places=2)


@given(triggers, ticks)
def test_gtt_limit_offset(trigger: Decimal, tick: Decimal) -> None:
    """HAT-EXEC-062: for every trigger T > 0 and tick, the limit is T x 0.99 rounded down to the
    tick and below T; when that is not a positive price the stop is refused, not guessed."""
    try:
        limit = gtt_limit_price(trigger, tick)
    except GttPriceError:
        assert (trigger * Decimal("0.99") // tick) * tick <= 0
        return
    exact = trigger * Decimal("0.99")
    assert limit % tick == 0
    assert limit <= exact < limit + tick  # the largest tick multiple not above T x 0.99
    assert 0 < limit < trigger


@given(st.one_of(st.just(Decimal(0)), st.decimals(max_value=Decimal(0), allow_nan=False)), ticks)
def test_gtt_limit_refuses_a_non_positive_trigger(trigger: Decimal, tick: Decimal) -> None:
    with pytest.raises(GttPriceError):
        gtt_limit_price(trigger, tick)
