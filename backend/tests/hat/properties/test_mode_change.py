"""PBT mode.*: mode changes queue outside market hours."""

from datetime import datetime, time, timedelta

import pytest
from hat_risk_support import CALENDAR, TRADE_DAY
from hypothesis import given
from hypothesis import strategies as st

from app.hat.data.sessions import IST
from app.hat.risk.modes import Mode, ModeChange, ModeChangeError, plan_mode_change, queued_mode_at

instants = st.datetimes(
    min_value=datetime(2026, 10, 12), max_value=datetime(2026, 10, 30), timezones=st.just(IST)
)
MODE = st.sampled_from(tuple(Mode))


@given(st.text(max_size=6), instants)
def test_unknown_mode_refused(mode: str, at: datetime) -> None:
    if mode in Mode:
        return
    evening = datetime.combine(TRADE_DAY, time(20), tzinfo=IST)
    with pytest.raises(ModeChangeError):
        plan_mode_change(mode, evening, CALENDAR)


@given(st.lists(st.tuples(MODE, st.integers(0, 12)), max_size=6), st.integers(0, 12))
def test_queued_mode_at(changes: list, probe: int) -> None:
    """The mode in force is the initial one until a change's effective time, then the latest
    such change's; a queued change alone never alters it."""
    base = datetime.combine(TRADE_DAY, time(20), tzinfo=IST)
    queued = [ModeChange(m, base, base + timedelta(days=d)) for m, d in changes]
    at = base + timedelta(days=probe)
    due = [c for c in queued if c.effective_from <= at]
    expected = "shadow"
    if due:
        expected = max(due, key=lambda c: (c.effective_from, c.requested_at)).to_mode
    assert queued_mode_at("shadow", queued, at) == expected
    assert queued_mode_at("shadow", queued, base - timedelta(days=1)) == "shadow"
