"""PBT mode.*: mode changes queue outside market hours."""

from datetime import datetime, time, timedelta

import pytest
from hat_risk_support import AT, CALENDAR, TRADE_DAY, seed_bot
from hat_support import memory_engine
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.data.sessions import IST
from app.hat.models import HatEvent, HatModeChange
from app.hat.risk.controls import queue_mode_change
from app.hat.risk.modes import ModeChange, ModeChangeError, effective_mode, plan_mode_change
from app.hat.risk.sessions import SessionState
from app.hat.time import as_utc

instants = st.datetimes(
    min_value=datetime(2026, 10, 12), max_value=datetime(2026, 10, 30), timezones=st.just(IST)
)
MODE = st.sampled_from(("shadow", "confirm", "auto"))


@given(MODE, instants)
def test_mode_change_next_session(to_mode: str, at: datetime) -> None:
    """A change requested while the market is closed takes effect at the next session open and
    never sooner; one requested inside market hours (or with an unknown date) is refused."""
    state = CALENDAR.state(at)
    if state is not SessionState.CLOSED:
        with pytest.raises(ModeChangeError):
            plan_mode_change(to_mode, at, CALENDAR)
        return
    change = plan_mode_change(to_mode, at, CALENDAR)
    assert change.effective_from > at
    assert CALENDAR.state(change.effective_from) is SessionState.OPEN
    # no session opens between the request and the effective time
    between = [
        d
        for d, row in CALENDAR.days.items()
        if row.is_trading
        and at < datetime.combine(d, time(9, 15), tzinfo=IST) < change.effective_from
    ]
    assert between == []


@given(st.text(max_size=6), instants)
def test_unknown_mode_refused(mode: str, at: datetime) -> None:
    if mode in ("shadow", "confirm", "auto"):
        return
    evening = datetime.combine(TRADE_DAY, time(20), tzinfo=IST)
    with pytest.raises(ModeChangeError):
        plan_mode_change(mode, evening, CALENDAR)


@given(st.lists(st.tuples(MODE, st.integers(0, 12)), max_size=6), st.integers(0, 12))
def test_effective_mode(changes: list, probe: int) -> None:
    """The mode in force is the initial one until a change's effective time, then the latest
    such change's; a queued change alone never alters it."""
    base = datetime.combine(TRADE_DAY, time(20), tzinfo=IST)
    queued = [ModeChange(m, base, base + timedelta(days=d)) for m, d in changes]
    at = base + timedelta(days=probe)
    due = [c for c in queued if c.effective_from <= at]
    expected = "shadow"
    if due:
        expected = max(due, key=lambda c: (c.effective_from, c.requested_at)).to_mode
    assert effective_mode("shadow", queued, at) == expected
    assert effective_mode("shadow", queued, base - timedelta(days=1)) == "shadow"


@given(MODE, st.booleans())
def test_queue_mode_change_writes_event(to_mode: str, inside: bool) -> None:
    """The service queues the row and `mode.change_requested` together, leaves the bot's mode
    as it was, and writes nothing when refused."""
    with Session(memory_engine()) as session:
        bot = seed_bot(session)
        at = AT if inside else datetime.combine(TRADE_DAY, time(20), tzinfo=IST)
        if inside:
            with pytest.raises(ModeChangeError):
                queue_mode_change(
                    session,
                    bot_id=bot,
                    to_mode=to_mode,
                    requested_at=at,
                    calendar=CALENDAR,
                    via="telegram",
                    chat_id=1,
                )
            assert session.scalars(select(HatModeChange)).all() == []
            assert session.scalars(select(HatEvent)).all() == []
            return
        row = queue_mode_change(
            session,
            bot_id=bot,
            to_mode=to_mode,
            requested_at=at,
            calendar=CALENDAR,
            via="dashboard",
            chat_id=1,
        )
        session.commit()
        assert as_utc(row.effective_from) > at
        events = session.scalars(select(HatEvent)).all()
        assert [e.event_type for e in events] == ["mode.change_requested"]
        from app.hat.models import HatBot

        assert session.get(HatBot, bot).execution_mode == "shadow"
