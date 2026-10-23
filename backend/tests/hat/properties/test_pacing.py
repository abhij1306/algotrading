"""The call pacer's timing rules on a fake clock: the interval between starts, the per-minute
window, the IST day budget and the state a restart shares."""

from datetime import UTC, datetime, timedelta

import pytest

from app.hat.clock import FixedClock
from app.hat.data.pacing import DayLimitError, MemoryState, Pacer

START = datetime(2026, 10, 23, 4, 0, tzinfo=UTC)  # 09:30 IST


def _pacer(clock: FixedClock, slept: list[float], **rules: object) -> Pacer:
    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(timedelta(seconds=seconds))

    return Pacer(clock=clock, sleep=sleep, **rules)  # type: ignore[arg-type]


def test_min_interval_spaces_starts_and_the_first_never_waits() -> None:
    clock, slept = FixedClock(START), []
    pacer = _pacer(clock, slept, min_interval=1.0)
    pacer.wait()
    clock.advance(timedelta(seconds=0.25))  # the call took a quarter second
    pacer.wait()
    clock.advance(timedelta(seconds=3))  # a slow call: no wait after it
    pacer.wait()
    assert slept == [0.75]
    assert pacer.made == 3


def test_per_minute_waits_for_the_oldest_start_to_leave_the_window() -> None:
    clock, slept = FixedClock(START), []
    pacer = _pacer(clock, slept, per_minute=2)
    pacer.wait()
    clock.advance(timedelta(seconds=10))
    pacer.wait()
    pacer.wait()  # the third in the minute waits until the first is 60 s old
    assert slept == [50.0]


def test_per_day_counts_the_ist_day_and_refuses_one_more() -> None:
    clock, slept = FixedClock(datetime(2026, 10, 23, 18, 29, tzinfo=UTC)), []  # 23:59 IST
    state = MemoryState(calls={"2026-10-23": 1})
    pacer = _pacer(clock, slept, per_day=2, state=state)
    pacer.wait()
    with pytest.raises(DayLimitError):
        pacer.wait()
    assert state.calls == {"2026-10-23": 2}
    clock.advance(timedelta(minutes=1))  # 00:00 IST: a fresh day
    pacer.wait()
    assert state.calls["2026-10-24"] == 1
    assert pacer.last_day is not None
    assert pacer.last_day.isoformat() == "2026-10-24"


def test_a_restart_paces_against_the_saved_minute() -> None:
    clock, slept = FixedClock(START), []
    state = MemoryState()
    first = _pacer(clock, slept, per_minute=1, state=state)
    first.wait()
    clock.advance(timedelta(seconds=20))
    _pacer(clock, slept, per_minute=1, state=state).wait()
    assert slept == [40.0]
