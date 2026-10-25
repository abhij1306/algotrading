"""PBT risk_policy.cooling_off: risk increases wait 7 days; a decrease withdraws a pending
increase."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from app.hat.governance import cooling_off_satisfied
from app.hat.risk.policy import (
    DIRECTIONS,
    PolicyVersion,
    effective_version,
    seed_policy,
    withdrawn,
    withdrawn_by_decrease,
)

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


def test_a_later_decrease_withdraws_a_pending_increase() -> None:
    """Audit A2 F5: an increase asked on day 0 (effective day 7) and a decrease on day 2
    (effective day 3): the decrease withdraws the increase, so day 7 still runs the decrease."""
    day0 = datetime(2026, 11, 2, 12, tzinfo=UTC)
    seed = seed_policy()
    initial = PolicyVersion(uuid.uuid4(), 1, day0 - timedelta(days=30), None, seed)
    increase = PolicyVersion(uuid.uuid4(), 2, day0 + timedelta(days=7), None, seed)
    asked = day0 + timedelta(days=2)
    assert withdrawn_by_decrease([initial, increase], asked) == [increase]
    decrease = PolicyVersion(uuid.uuid4(), 3, day0 + timedelta(days=3), None, seed)
    pulled = replace(increase, superseded_at=asked)
    assert withdrawn(pulled.effective_from, pulled.superseded_at)
    assert not withdrawn(initial.effective_from, day0 + timedelta(days=3))
    versions = [initial, pulled, decrease]
    assert effective_version(versions, day0 + timedelta(days=8)) == decrease
    assert effective_version(versions, day0 + timedelta(days=1)) == initial
