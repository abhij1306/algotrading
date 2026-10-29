"""The daily data update's plan (`data.update.plan`): the step order, which steps a failure
skips, the sessions a step still has to fill, the weekly cadence and the status file. Small
hand-built inputs; no step runs."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from app.hat.data.update import plan
from app.hat.data.update.plan import FAILED, OK, SKIPPED, Step, StepStatus

MON, TUE, WED, THU, FRI = (date(2026, 10, d) for d in (26, 27, 28, 29, 30))
WEEK = [MON, TUE, WED, THU, FRI]


def test_the_step_table_is_in_dependency_order() -> None:
    plan.check_order(plan.STEPS)
    with pytest.raises(ValueError, match="out of order"):
        plan.check_order([Step("b", ("a",)), Step("a")])


def test_only_keeps_table_order_and_refuses_an_unknown_step() -> None:
    names = [s.name for s in plan.selected(plan.STEPS, ["features", "bars"])]
    assert names == ["bars", "features"]
    assert len(plan.selected(plan.STEPS)) == len(plan.STEPS)
    with pytest.raises(ValueError, match="unknown step"):
        plan.selected(plan.STEPS, ["nope"])


def test_a_step_is_blocked_by_a_failed_or_skipped_step_it_reads() -> None:
    features = Step("features", ("quality", "ca"))
    assert plan.blocked_by(features, {"quality": OK, "ca": OK}) is None
    assert plan.blocked_by(features, {"quality": FAILED, "ca": OK}) == "quality"
    assert plan.blocked_by(features, {"quality": OK, "ca": SKIPPED}) == "ca"
    assert plan.blocked_by(features, {}) is None  # not run in this run (--only): stored data


def test_the_latest_finished_session_turns_at_18_ist() -> None:
    assert plan.latest_finished(WEEK, datetime(2026, 10, 29, 17, 59)) == WED
    assert plan.latest_finished(WEEK, datetime(2026, 10, 29, 18, 0)) == THU
    assert plan.latest_finished(WEEK, datetime(2026, 11, 1, 9, 0)) == FRI  # a Sunday
    assert plan.latest_finished(WEEK, datetime(2026, 10, 1, 20, 0)) is None


def test_sessions_to_fill_catch_up_every_missed_session() -> None:
    assert plan.sessions_to_fill(WEEK, MON, THU) == [TUE, WED, THU]
    assert plan.sessions_to_fill(WEEK, THU, THU) == []
    assert plan.sessions_to_fill(WEEK, FRI, THU) == []
    assert plan.sessions_to_fill(WEEK, None, WED) == [WED]  # first run: the backfill is the tools'
    assert plan.sessions_to_fill(WEEK, None, date(2026, 10, 31)) == []


def test_a_weekly_step_is_due_a_week_after_its_last_success() -> None:
    assert plan.due(plan.DAILY, THU, THU)
    assert plan.due(plan.WEEKLY, None, THU)
    assert not plan.due(plan.WEEKLY, MON, date(2026, 11, 1))
    assert plan.due(plan.WEEKLY, MON, date(2026, 11, 2))


def test_the_status_file_reads_back_what_was_written(tmp_path: Path) -> None:
    path = tmp_path / "update" / "status.json"
    assert plan.read_status(path) == {}
    steps = {
        "bars": StepStatus(OK, "2026-10-29T19:31:00+05:30", "2026-10-29", "2026-10-29", "1", "b"),
        "features": StepStatus(SKIPPED, "2026-10-29T19:40:00+05:30", "2026-10-28"),
    }
    plan.write_status(path, steps, {"through": "2026-10-29"})
    assert plan.read_status(path) == steps


def test_the_exit_code_is_1_when_a_step_failed_or_was_skipped() -> None:
    assert plan.exit_code({"bars": OK, "results": plan.NOT_DUE}) == 0
    assert plan.exit_code({"bars": OK, "ca": FAILED}) == 1
    assert plan.exit_code({"features": SKIPPED}) == 1


def test_the_5m_check_covers_the_lookback_and_every_session_after_the_last_stored() -> None:
    assert plan.recent_sessions(WEEK, THU, FRI, 2) == [THU, FRI]
    assert plan.recent_sessions(WEEK, MON, FRI, 2) == [TUE, WED, THU, FRI]  # a long stop
    assert plan.recent_sessions(WEEK, None, WED, 2) == [TUE, WED]


def test_gaps_are_per_instrument_and_skip_ledgered_refusals() -> None:
    expected = {WED: {"a", "b", "c"}, THU: {"a", "b"}}
    stored = {WED: {"a"}, THU: {"a", "b", "z"}}  # one instrument's partition is not everyone's
    assert plan.gaps(expected, stored) == {WED: {"b", "c"}}
    assert plan.gaps(expected, stored, {("c", WED)}) == {WED: {"b"}}
    assert plan.gaps(expected, {WED: {"a", "b", "c"}, THU: {"a", "b"}}) == {}
