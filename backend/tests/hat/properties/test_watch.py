"""The health rules: the out-of-process watch's findings and their one-alert-per-event
transition, the broker outage alert, the alert text's action line and the partial fill note."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.hat.execution.session import unreachable_watched
from app.hat.ops.watch import (
    DISK_MIN_FREE_BYTES,
    HEARTBEAT_STALE,
    Finding,
    checked,
    disk_findings,
    heartbeat_findings,
    run_watch,
    save_state,
    transition,
    unwritable_exit,
    update_due,
    update_findings,
    verify_findings,
)
from app.hat.telegram import render
from app.hat.time import IST

GB = 1024**3
NIGHT = datetime(2026, 10, 29, 22, 0, tzinfo=IST)
EVENING = datetime(2026, 10, 29, 20, 0, tzinfo=IST)


def _status(started: str, failed: list[str] = (), skipped: list[str] = ()) -> dict:
    return {"run": {"started": started, "failed": list(failed), "skipped": list(skipped)}}


# --- the data update -------------------------------------------------------------------------


def test_update_without_status_file_is_not_watched() -> None:
    assert update_findings(None, NIGHT) == []


def test_update_failed_is_one_finding_per_run() -> None:
    found = update_findings(_status("2026-10-29T19:30:00+05:30", ["bars5m"], ["features"]), NIGHT)
    assert [f.code for f in found] == ["update.failed"]
    assert found[0].detail == {"failed.1": "bars5m", "skipped.1": "features"}
    later = update_findings(
        _status("2026-10-29T19:30:00+05:30", ["bars5m", "etf"], ["features"]), NIGHT
    )
    assert later[0].key == found[0].key  # a worse run is the same event, not resolved and new
    tomorrow = update_findings(_status("2026-10-30T19:30:00+05:30", ["bars5m"]), NIGHT)
    assert tomorrow[0].key != found[0].key  # the next run is a new one


def test_update_status_of_another_shape_is_read_as_no_run() -> None:
    found = update_findings({"run": ["not", "a", "record"]}, NIGHT)
    assert [f.code for f in found] == ["update.late"]
    found = update_findings({"run": {"started": "2026-10-29T19:30:00+05:30", "failed": 3}}, NIGHT)
    assert found == []


def test_update_clean_run_today_is_no_finding() -> None:
    assert update_findings(_status("2026-10-29T19:30:00+05:30"), NIGHT) == []


def test_update_late_is_keyed_on_the_due_run_date() -> None:
    yesterday = _status("2026-10-28T19:30:00+05:30")
    assert update_findings(yesterday, EVENING) == []  # today's run is not due before 21:30
    found = update_findings(yesterday, NIGHT)
    assert [(f.code, f.detail) for f in found] == [
        ("update.late", {"due": "2026-10-29", "last_run": "2026-10-28"})
    ]
    # past midnight the same date is still the one due: the same event, never resolved
    after_midnight = datetime(2026, 10, 30, 0, 5, tzinfo=IST)
    assert [f.key for f in update_findings(yesterday, after_midnight)] == [found[0].key]
    assert update_due(after_midnight) == date(2026, 10, 29)


def test_unreadable_status_file_is_late() -> None:
    found = update_findings({}, NIGHT)
    assert [(f.code, f.detail["last_run"]) for f in found] == [("update.late", "never")]


def test_a_check_that_raises_is_its_own_finding() -> None:
    def broken() -> list[Finding]:
        raise KeyError("run")

    found = checked("update", broken)
    assert [(f.code, f.key, dict(f.detail)) for f in found] == [
        ("watch.check_error", "watch.check_error:update", {"check": "update", "error": "KeyError"})
    ]


# --- the data verification -------------------------------------------------------------------


def _report(created: str, digest: str, **checks: str) -> dict:
    return {
        "created": created,
        "digest": digest,
        "checks": {k: {"status": v} for k, v in checks.items()},
    }


def test_verify_newest_report_of_each_check_decides() -> None:
    reports = [
        ("verify-2026-10-20", _report("2026-10-20T10:00", "a", D1="pass", D3="fail")),
        ("verify-2026-10-28-D3", _report("2026-10-28T10:00", "b", D1="skipped", D3="pass")),
        ("verify-2026-10-29-D7", _report("2026-10-29T10:00", "c", D7="fail")),
    ]
    found = verify_findings(reports)
    assert [(f.detail["check"], f.detail["report"]) for f in found] == [
        ("D7", "verify-2026-10-29-D7")
    ]
    assert found[0].key == "verify.failed:D7:c"


def test_verify_a_skipped_check_does_not_hide_an_older_failure() -> None:
    reports = [
        ("verify-old", _report("2026-10-20T10:00", "a", D2="fail")),
        ("verify-new", _report("2026-10-29T10:00", "b", D2="skipped")),
    ]
    assert [f.key for f in verify_findings(reports)] == ["verify.failed:D2:a"]


# --- disk and heartbeat ----------------------------------------------------------------------


def test_disk_low_by_bytes_or_by_share() -> None:
    usage = {
        "root": (50 * GB, 100 * GB),
        "data_root": (DISK_MIN_FREE_BYTES - 1, 1000 * GB),
        "small": (11 * GB, 200 * GB),  # 5.5 % free
    }
    found = disk_findings(usage)
    assert [(f.detail["disk"], f.detail["free_gb"], f.detail["free_pct"]) for f in found] == [
        ("data_root", 9, 0),
        ("small", 11, 5),
    ]


def test_heartbeat_stale_or_missing_is_runtime_down() -> None:
    now = datetime(2026, 10, 29, 4, 0, tzinfo=UTC)
    assert heartbeat_findings(now - HEARTBEAT_STALE, now) == []
    stale = heartbeat_findings(now - HEARTBEAT_STALE - timedelta(seconds=1), now)
    assert [(f.code, f.detail["last_beat"]) for f in stale] == [
        ("runtime.down", "2026-10-29T09:26")
    ]
    assert heartbeat_findings(None, now)[0].detail == {"last_beat": "never"}


# --- one alert per event ---------------------------------------------------------------------


def test_transition_tells_each_event_once_and_its_clearing() -> None:
    disk = Finding("disk.low", "disk.low:root", {"disk": "root"})
    db = Finding("db.down", "db.down:state", {"database": "state"})
    first = transition({}, [disk, db, disk])
    assert first.new == [disk, db]
    assert first.cleared == []
    open_ = {f.key: {"code": f.code} for f in first.new}
    assert transition(open_, [disk, db]).new == []  # still standing: nothing more
    step = transition(open_, [disk])
    assert step.new == []
    assert step.cleared == [("db.down:state", {"code": "db.down"})]


class _Marks:
    def __init__(self, *, broken: bool = False) -> None:
        self.claimed: set[str] = set()
        self.broken = broken  # the store cannot be read (hat_state down): every claim is None

    def claim(self, name: str) -> bool | None:
        if self.broken:
            return None
        if name in self.claimed:
            return False
        self.claimed.add(name)
        return True

    def clear(self, name: str) -> None:
        self.claimed.discard(name)


def _sender(sent: list[str], ok: bool = True):  # noqa: ANN202 - a test double
    def send(text: str) -> bool:
        sent.append(text)
        return ok

    return send


def test_state_is_saved_before_anything_is_sent() -> None:
    disk = Finding("disk.low", "disk.low:root", {"disk": "root"})
    saved: list[dict] = []
    sent: list[str] = []

    def save(doc: dict) -> bool:
        saved.append(dict(doc))
        assert sent == []  # nothing told before the state that follows is on disk
        return True

    outcome = run_watch({}, [disk], NIGHT, _sender(sent), save, _Marks())
    assert list(outcome.open) == ["disk.low:root"]
    assert outcome.exit_code == 0
    assert list(saved[0]) == ["disk.low:root"]
    assert sent[0].startswith("Alert: disk.low (2026-10-29)")
    assert sent[0].splitlines()[-1] == f"Do: {render.ACTIONS['disk.low']}"


def test_an_unsent_alert_is_tried_again_next_run() -> None:
    disk = Finding("disk.low", "disk.low:root", {"disk": "root"})
    saved: list[dict] = []

    def save(doc: dict) -> bool:
        saved.append(dict(doc))
        return True

    assert run_watch({}, [disk], NIGHT, _sender([], ok=False), save, _Marks()).open == {}
    assert saved[-1] == {}  # saved again without it, so the next run tells it


def test_unwritable_state_tells_one_alert_and_nothing_else() -> None:
    disk = Finding("disk.low", "disk.low:root", {"disk": "root"})
    db = Finding("db.down", "db.down:state", {"database": "state"})
    marks, sent = _Marks(), []
    for _ in range(3):  # three runs on a full disk
        outcome = run_watch({}, [disk, db], NIGHT, _sender(sent), lambda d: False, marks)
        assert (outcome.open, outcome.exit_code) == (None, 0)
    assert [t.splitlines()[0] for t in sent] == ["URGENT: watch.state_unwritable (2026-10-29)"]
    # a save that works again clears the claim; the findings are then told once
    outcome = run_watch({}, [disk], NIGHT, _sender(sent), lambda d: True, marks)
    assert list(outcome.open) == ["disk.low:root"]
    assert marks.claimed == set()


def test_unwritable_state_and_hat_state_down_exits_non_zero() -> None:
    """Both stores down: nothing can be told or recorded, so the run fails and the unit's
    OnFailure= (at most once a day) tells the owner, instead of silence."""
    disk = Finding("disk.low", "disk.low:root", {"disk": "root"})
    sent: list[str] = []
    outcome = run_watch({}, [disk], NIGHT, _sender(sent), lambda d: False, _Marks(broken=True))
    assert (outcome.open, outcome.exit_code, sent) == (None, 1, [])


def test_unwritable_exit_decision() -> None:
    assert unwritable_exit(None) == 1  # the claim unreadable: hat_state down as well
    assert unwritable_exit(True) == 0  # the one alert was claimed and sent now
    assert unwritable_exit(False) == 0  # it went out on an earlier run


def test_resolved_names_its_subject() -> None:
    disk = Finding("disk.low", "disk.low:root", {"disk": "root", "free_gb": 3})
    sent: list[str] = []
    after = run_watch({}, [disk], NIGHT, _sender(sent), lambda d: True, _Marks()).open
    run_watch(after, [], NIGHT, _sender(sent), lambda d: True, _Marks())
    assert sent[-1].splitlines() == ["Resolved: disk.low (2026-10-29)", "disk: root", "free_gb: 3"]


def test_save_state_is_atomic_and_reports_failure(tmp_path: Path) -> None:
    path = tmp_path / "ops" / "watch.json"
    assert save_state(path, {"k": {"code": "disk.low"}})
    assert json.loads(path.read_text())["open"] == {"k": {"code": "disk.low"}}
    (tmp_path / "blocked").write_text("a file, not a folder")
    assert not save_state(tmp_path / "blocked" / "watch.json", {})
    assert [p.name for p in path.parent.iterdir()] == ["watch.json"]  # no temporary left


# --- the broker outage -----------------------------------------------------------------------


def test_unreachable_is_watched_from_the_login_prompt_to_the_close() -> None:
    def at(h: int, m: int) -> datetime:
        return datetime(2026, 10, 29, h, m, tzinfo=IST)

    assert not unreachable_watched(at(8, 29))
    assert unreachable_watched(at(8, 30))
    assert unreachable_watched(at(15, 29))
    assert not unreachable_watched(at(15, 30))
    assert not unreachable_watched(at(2, 0))


# --- the text --------------------------------------------------------------------------------


def test_alert_text_ends_with_what_to_do() -> None:
    text = render.alert_text("broker.unreachable", date(2026, 10, 29), {"minutes": 3})
    assert text.splitlines() == [
        "URGENT: broker.unreachable (2026-10-29)",
        "minutes: 3",
        f"Do: {render.ACTIONS['broker.unreachable']}",
    ]
    unknown = render.alert_text("something.new", date(2026, 10, 29), {})
    assert unknown.splitlines()[-1] == f"Do: {render.ACTION_DEFAULT}"


def test_fill_text_says_when_its_order_is_part_filled() -> None:
    def view(
        filled: int | None, ordered: int | None, working: bool = True
    ) -> render.PositionEventView:
        return render.PositionEventView(
            "ABC",
            40,
            Decimal("100"),
            Decimal("0.1"),
            filled=filled,
            ordered=ordered,
            working=working,
        )

    part = render.position_event_text("order.filled", view(40, 100))
    assert part.splitlines()[-1] == "Partial: 40 of 100 filled; the rest still works"
    ended = render.position_event_text("order.filled", view(40, 100, working=False))
    assert ended.splitlines()[-1] == "Partial: 40 of 100 filled; the rest will not fill"
    whole = render.position_event_text("order.filled", view(100, 100))
    assert "Partial" not in whole
    assert "Partial" not in render.position_event_text("stop.moved", view(None, None))
