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
    places=st.integers(2, 5),  # a factor sent short; a non-exact one is then 1e-8 or more off
)
def test_factor_exact(d: int, n: int, places: int) -> None:
    """For every ratio n/d below 1 with d up to 1000, the stored 12-place factor gives back n/d
    exactly; and a factor sent to fewer places that is not exactly such a ratio is refused (the
    position stays manage-only) rather than read as some nearby ratio. Harm: 0.0110 read as
    11/1000 instead of 1/91 sizes the rescaled position at a tenth of its shares."""
    assume(n < d)
    f = Fraction(n, d)
    assert rescale.exact_factor(factor_to_decimal(f)) == f
    short = (D(n) / D(d)).quantize(D(1).scaleb(-places))
    nearest = Fraction(short).limit_denominator(rescale.MAX_DENOMINATOR)
    if nearest != Fraction(short):
        stored = factor_to_decimal(Fraction(short))
        with pytest.raises(ValueError):
            rescale.exact_factor(stored)


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
