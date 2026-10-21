"""PBT time.trade_date_ist (HAT-SCH-015): the trade date is the Asia/Kolkata calendar date."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.hat.time import as_utc, trade_date_of, utc_now

# Aware instants in any fixed or named zone, kept away from the datetime range limits.
aware = st.datetimes(
    min_value=datetime(1971, 1, 1),
    max_value=datetime(8999, 12, 31),
    allow_imaginary=False,  # a wall time skipped by a daylight saving jump is no instant
    timezones=st.one_of(
        st.timezones(),
        st.just(UTC),
        st.integers(min_value=-14 * 60, max_value=14 * 60).map(
            lambda minutes: timezone(timedelta(minutes=minutes))
        ),
    ),
)


@given(instant=aware)
def test_trade_date_ist(instant: datetime) -> None:
    """For every instant, the trade date is UTC plus 5h30 (IST has no daylight saving)."""
    expected = (instant.astimezone(UTC) + timedelta(hours=5, minutes=30)).date()
    assert trade_date_of(instant) == expected


@given(instant=st.datetimes(min_value=datetime(1971, 1, 1), max_value=datetime(8999, 12, 31)))
def test_trade_date_rejects_naive_instants(instant: datetime) -> None:
    """A naive datetime has no instant, so it is refused instead of guessed."""
    with pytest.raises(ValueError, match="timezone-aware"):
        trade_date_of(instant)


@given(instant=aware)
def test_as_utc_keeps_the_instant(instant: datetime) -> None:
    converted = as_utc(instant)
    # `==` between two datetimes in one zone ignores the fold, so compare epoch seconds.
    assert converted.timestamp() == instant.timestamp()
    assert converted.utcoffset() == timedelta(0)
    assert as_utc(instant.replace(tzinfo=None)) == instant.replace(tzinfo=UTC)


def test_utc_now_is_aware_utc() -> None:
    assert utc_now().utcoffset() == timedelta(0)
