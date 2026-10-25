"""The rescale of a position across a split or bonus: the stored factor read back as an exact
small ratio, the position scaled by it (quantity ⌊q / f⌋, prices × f, both stops floored), and a
live price that reads as an unstored split. Pure functions, no database."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.hat.data.corporate_actions import factor_to_decimal
from app.hat.engine import rescale

D = Decimal


@settings(max_examples=200, deadline=None)
@given(
    d=st.integers(2, rescale.MAX_DENOMINATOR),
    n=st.integers(1, rescale.MAX_DENOMINATOR - 1),
)
def test_factor_exact(d: int, n: int) -> None:
    """For every ratio n/d below 1 with d up to 1000, the stored 12-place factor gives back n/d
    exactly; and a factor sent to 5 places that is not exactly such a ratio is refused (the
    position stays manage-only) rather than read as some nearby ratio. Harm: a guessed ratio
    sizes the rescaled position wrongly."""
    assume(n < d)
    f = Fraction(n, d)
    assert rescale.exact_factor(factor_to_decimal(f)) == f
    short = Fraction((D(n) / D(d)).quantize(D("0.00001")))
    five = (short * 10**4).denominator != 1  # not a factor sent to 4 places
    if five and short.limit_denominator(rescale.MAX_DENOMINATOR) != short:
        stored = factor_to_decimal(short)
        with pytest.raises(ValueError):
            rescale.exact_factor(stored)


@settings(max_examples=200, deadline=None)
@given(d=st.integers(2, rescale.ROUNDED_DENOMINATOR), n=st.integers(1, 98))
def test_factor_four_places(d: int, n: int) -> None:
    """A ratio with a denominator up to 99 sent to 4 places (the MCP's 0.6667 for 2/3) gives back
    the ratio, unless the 4 places are themselves an exact ratio (0.1250 is 1/8). Harm: 300
    stored splits and bonuses left every position through them manage-only."""
    assume(n < d)
    four = (D(n) / D(d)).quantize(D("0.0001"))
    assume(Fraction(four).limit_denominator(rescale.MAX_DENOMINATOR) != Fraction(four))
    assert rescale.exact_factor(factor_to_decimal(Fraction(four))) == Fraction(n, d)


@pytest.mark.parametrize(
    ("stored", "ratio"),
    [
        ("0.6667", Fraction(2, 3)),
        ("0.3333", Fraction(1, 3)),
        ("0.9091", Fraction(10, 11)),
        ("0.8333", Fraction(5, 6)),
        ("0.7143", Fraction(5, 7)),
        ("0.5", Fraction(1, 2)),
    ],
)
def test_factor_mcp_rounded(stored: str, ratio: Fraction) -> None:
    """The factors hat_live holds most often, as the MCP stored them."""
    assert rescale.exact_factor(D(stored)) == ratio


@pytest.mark.parametrize("stored", ["0.66667", "0.6668", "0.666670000001"])
def test_factor_not_rounded_refused(stored: str) -> None:
    """Past 4 places, or 4 places no small ratio rounds to, the factor is refused."""
    factor = D(stored)
    with pytest.raises(ValueError):
        rescale.exact_factor(factor)


@pytest.mark.parametrize(
    ("price", "previous", "split"),
    [
        ("50", "100", True),  # a 1:2 split
        ("60", "100", True),
        ("60.01", "100", False),
        ("80", "100", False),  # a 20% band move
        ("1", "0", False),  # no previous close: never a gap
    ],
)
def test_split_like(price: str, previous: str, split: bool) -> None:
    """A live price at or below 0.6 of the previous raw close reads as an unstored split (Q3)."""
    assert rescale.split_like(D(price), D(previous)) is split


@pytest.mark.parametrize(
    ("isin", "watched"),
    [
        ("INE234I01028", True),  # equity shares
        ("INE0ABC20012", False),  # a rights entitlement (type 20, the -RE symbols)
        (None, True),  # an unknown instrument stays watched
    ],
)
def test_split_gap_watched(isin: str | None, watched: bool) -> None:
    """The split-gap guard skips only rights entitlements (owner 2026-10-25)."""
    assert rescale.split_gap_watched(isin) is watched


@pytest.mark.parametrize(
    ("qty", "f", "held"),
    [
        (100, Fraction(1, 2), 200),  # a 1:2 split
        (100, Fraction(1, 5), 500),
        (100, Fraction(2, 3), 150),  # a 1:2 bonus
        (101, Fraction(2, 3), 151),  # 151.5: the half share is paid in cash
        (7, Fraction(10, 11), 7),  # 7.7
        (0, Fraction(1, 2), 0),
        (-3, Fraction(1, 2), 0),
    ],
)
def test_scale_qty_floors(qty: int, f: Fraction, held: int) -> None:
    assert rescale.scale_qty(qty, f) == held


@pytest.mark.parametrize(
    ("before", "f", "after"),
    [
        (
            (100, "100", "90", "95"),
            Fraction(1, 2),
            rescale.Scaled(200, D("50.0000"), D("45.0000"), D("47.5000")),
        ),
        (
            (101, "100", "90", "95.0005"),
            Fraction(2, 3),
            # 66.66666.. half-even; 60 and 63.333666.. floored, never above the exact stop
            rescale.Scaled(151, D("66.6667"), D("60.0000"), D("63.3336")),
        ),
        (
            # a current stop one place above the initial: both floor to the same value
            (10, "100", "90", "90.0001"),
            Fraction(2, 3),
            rescale.Scaled(15, D("66.6667"), D("60.0000"), D("60.0000")),
        ),
        (
            (30, "123.4567", "101.0001", "101.0001"),
            Fraction(10, 11),
            rescale.Scaled(33, D("112.2334"), D("91.8182"), D("91.8182")),
        ),
    ],
)
def test_scale_position(before: tuple, f: Fraction, after: rescale.Scaled) -> None:
    """Quantity ⌊q / f⌋, entry × f to the stored places, both stops × f floored the same way so
    `current_stop >= initial_stop` still holds (the DB trigger refuses it otherwise)."""
    qty, entry, initial, current = before
    scaled = rescale.scale_position(qty, D(entry), D(initial), D(current), f)
    assert scaled == after
    assert scaled.current_stop >= scaled.initial_stop
    assert scaled.current_stop <= D(current) * f.numerator / f.denominator
