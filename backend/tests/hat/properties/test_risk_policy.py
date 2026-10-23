"""PBT risk_policy.cooling_off: risk increases wait 7 days."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from hat_support import memory_engine
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy.orm import Session

from app.hat.models import HatRiskPolicyVersion
from app.hat.risk.policy import DIRECTIONS, CoolingOffError, cooling_off_satisfied

SEVEN_DAYS_US = 7 * 24 * 3600 * 1_000_000
requested = st.datetimes(
    min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1), timezones=st.just(UTC)
)
# Offsets in microseconds, dense around the 7-day boundary and spread far on both sides.
offsets = st.one_of(
    st.integers(min_value=SEVEN_DAYS_US - 3, max_value=SEVEN_DAYS_US + 3),
    st.integers(min_value=-(10**13), max_value=10**13),
)


@given(direction=st.sampled_from(DIRECTIONS), asked=requested, offset_us=offsets)
def test_cooling_off(direction: str, asked: datetime, offset_us: int) -> None:
    """An increase is allowed iff it takes effect at least 7 days after the request; an
    initial or decrease version is never delayed."""
    effective = asked + timedelta(microseconds=offset_us)
    expected = direction != "increase" or offset_us >= SEVEN_DAYS_US
    assert cooling_off_satisfied(direction, asked, effective) is expected


ENGINE = memory_engine(HatRiskPolicyVersion)


@given(direction=st.sampled_from(DIRECTIONS), asked=requested, offset_us=offsets)
def test_cooling_off_enforced_on_write(direction: str, asked: datetime, offset_us: int) -> None:
    """The model rejects a too-early increase at flush, as the Postgres CHECK does."""
    row = HatRiskPolicyVersion(
        user_id=uuid.uuid4(),
        version=1,
        policy={},
        direction=direction,
        requested_at=asked,
        effective_from=asked + timedelta(microseconds=offset_us),
    )
    allowed = direction != "increase" or offset_us >= SEVEN_DAYS_US
    with Session(ENGINE) as session:
        session.add(row)
        if allowed:
            session.commit()
            session.delete(row)  # rows stay out of the next example
            session.commit()
        else:
            with pytest.raises(CoolingOffError):
                session.flush()
            session.rollback()
