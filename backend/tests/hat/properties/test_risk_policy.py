"""PBT risk_policy.cooling_off: risk increases wait 7 days."""

from datetime import UTC, datetime, timedelta

from hat_support import memory_engine
from hypothesis import given
from hypothesis import strategies as st

from app.hat.governance import cooling_off_satisfied
from app.hat.models import HatRiskPolicyVersion
from app.hat.risk.policy import DIRECTIONS

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
