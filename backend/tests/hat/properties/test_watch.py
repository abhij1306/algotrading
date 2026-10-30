"""The health rules: the out-of-process watch's findings and their one-alert-per-event
transition, the broker outage alert, the alert text's action line and the partial fill note."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.hat.execution.session import UNREACHABLE_AFTER, unreachable_step
from app.hat.ops.watch import (
    DISK_MIN_FREE_BYTES,
    HEARTBEAT_STALE,
    Finding,
    disk_findings,
    heartbeat_findings,
    tell,
    transition,
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


def test_update_failed_steps_are_one_finding_per_run_and_steps() -> None:
    found = update_findings(_status("2026-10-29T19:30:00+05:30", ["bars5m"], ["features"]), NIGHT)
    assert [f.code for f in found] == ["update.failed"]
    assert found[0].detail == {"failed.1": "bars5m", "skipped.1": "features"}
    later = update_findings(
        _status("2026-10-29T19:30:00+05:30", ["bars5m", "etf"], ["features"]), NIGHT
    )
    assert later[0].key != found[0].key  # a step that failed later in the run is told
    again = update_findings(_status("2026-10-29T19:30:00+05:30", ["bars5m"], ["features"]), NIGHT)
    assert again[0].key == found[0].key  # the same run and steps: the same event


def test_update_clean_run_today_is_no_finding() -> None:
    assert update_findings(_status("2026-10-29T19:30:00+05:30"), NIGHT) == []


def test_update_late_only_after_the_deadline() -> None:
    yesterday = _status("2026-10-28T19:30:00+05:30")
    assert update_findings(yesterday, EVENING) == []  # still before 21:30
    found = update_findings(yesterday, NIGHT)
    assert [(f.code, f.detail) for f in found] == [("update.late", {"last_run": "2026-10-28"})]
    assert found[0].key == "update.late:2026-10-29"


def test_unreadable_status_file_is_late() -> None:
    found = update_findings({}, NIGHT)
    assert [(f.code, f.detail["last_run"]) for f in found] == [("update.late", "never")]


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


def test_tell_keeps_an_unsent_alert_unopened() -> None:
    disk = Finding("disk.low", "disk.low:root", {"disk": "root"})
    sent: list[str] = []
    after = tell({}, [disk], NIGHT, lambda text: False)
    assert after == {}  # tried again at the next run
    after = tell({}, [disk], NIGHT, lambda text: sent.append(text) or True)
    assert list(after) == ["disk.low:root"]
    assert sent[0].startswith("Alert: disk.low (2026-10-29)")
    assert sent[0].splitlines()[-1] == f"Do: {render.ACTIONS['disk.low']}"
    tell(after, [], NIGHT, lambda text: sent.append(text) or True)
    assert sent[-1] == "Resolved: disk.low (2026-10-29)"


# --- the broker outage -----------------------------------------------------------------------


def test_unreachable_is_one_alert_per_outage() -> None:
    short = UNREACHABLE_AFTER - timedelta(seconds=1)
    assert unreachable_step(short, False) == (False, False)
    assert unreachable_step(UNREACHABLE_AFTER, False) == (True, True)
    assert unreachable_step(UNREACHABLE_AFTER * 3, True) == (False, True)
    assert unreachable_step(timedelta(0), True) == (False, False)  # a success ends it
    assert unreachable_step(UNREACHABLE_AFTER, False) == (True, True)  # the next outage
    # a success and a new outage between two checks: the shorter outage is a new one
    assert unreachable_step(UNREACHABLE_AFTER, True, UNREACHABLE_AFTER * 3) == (True, True)
    assert unreachable_step(UNREACHABLE_AFTER * 2, True, UNREACHABLE_AFTER) == (False, True)


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
