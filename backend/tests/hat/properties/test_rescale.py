"""PBT rescale.*: an open position spanning a split or bonus is
rescaled before the ex-date open (quantity ⌊q / f⌋, prices × f, once per action, with its event),
no exit fires from the action alone, the instrument is manage-only until the rescale is done, the
broker stop follows it down, and the night before looks the next session's actions up for the held
and watchlisted instruments. Real engine, SQLite in memory, `FakeBroker`; no network."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from hat_risk_support import TRADE_DAY
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session

from app.hat.data.corporate_actions import (
    factor_to_decimal,
)
from app.hat.engine import exits, rescale
from app.hat.models import (
    HatCorporateAction,
)
from app.hat.time import IST

D = Decimal
EX_DAY = TRADE_DAY + timedelta(days=1)  # Thursday: the ex-date after an entry on Wednesday
OPEN = datetime.combine(EX_DAY, time(9, 16), tzinfo=IST)


def _action(session: Session, instrument: uuid.UUID, ex: date, kind: str, f: Fraction) -> None:
    session.add(
        HatCorporateAction(
            instrument_id=instrument,
            ex_date=ex,
            action_type=kind,
            adjustment_factor=factor_to_decimal(f),
            raw_text="fixture",
            source="mcp",
        )
    )
    session.commit()


# --- the rescale keeps the position -----------------------------------------------


# --- each action once, never for a position opened on or after it -----------------


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
        with pytest.raises(ValueError):
            rescale.exact_factor(factor_to_decimal(short))


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
    with pytest.raises(ValueError):
        rescale.exact_factor(D(stored))


# --- until rescaled, no exit and no entry ---------------------------------------


class _Book:
    """The daily levels of the exits: a fixed view for any instrument."""

    def day(self, _day: date) -> _Book:
        return self

    def exit_view(
        self, _iid: uuid.UUID, price: Decimal, _rules: object, held: int
    ) -> exits.ExitView:
        return exits.ExitView(price=price, days_held=held, sma={10: price})


# --- an action the rescale cannot handle leaves the position manage-only -----------

# --- the broker stop follows the rescale down ---------------------------------------


# --- the night before ---------------------------------------------------------------
