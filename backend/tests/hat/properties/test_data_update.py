"""The daily data update's plan (`data.update.plan`): the step order, which steps a failure
skips, the sessions a step still has to fill, the weekly cadence and the status file. Small
hand-built inputs; no step runs."""

from __future__ import annotations

import shlex
from datetime import date, datetime
from pathlib import Path

import pytest

from app.hat.data.update import plan
from app.hat.data.update.plan import FAILED, MANUAL, OK, SKIPPED, WAITING, Step, StepStatus

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
    assert plan.blocked_by(features, {"quality": FAILED, "ca": OK}) == ("quality", SKIPPED)
    assert plan.blocked_by(features, {"quality": OK, "ca": SKIPPED}) == ("ca", SKIPPED)
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


def test_the_membership_load_names_the_history_inside_the_admin_folder() -> None:
    """The update copies the built history into the admin folder under a dated name and loads it
    by that name. Harm: the step refused `file_outside_admin` every night (an absolute work path),
    the membership never covering the new sessions."""
    from app.hat.data.update.steps import membership_admin_name, membership_load

    day = date(2026, 10, 30)
    assert membership_admin_name(day) == "nifty500-membership-2026-10-30.csv"
    argv = membership_load(day)
    at = argv.index("--csv")
    assert argv[at + 1] == "nifty500-membership-2026-10-30.csv"
    assert "/" not in argv[at + 1]
    assert argv[argv.index("--as-of") + 1] == "2026-10-30"
    assert argv[-1] == "--apply"


def test_a_step_reading_a_manual_one_waits_and_no_run_fails() -> None:
    """A step whose data waits on a file the owner downloads from NSE by hand holds back the
    steps that read it (`waiting`, and those after them), but the run does not exit 1. Harm:
    `features` run on bars without the night's split (a 1:5 split read as -80% by the scanners),
    or one missing hand file failing the timer every night."""
    features = Step("features", ("quality", "ca"))
    assert plan.blocked_by(features, {"quality": OK, "ca": MANUAL}) == ("ca", WAITING)
    later = Step("later", ("features",))
    assert plan.blocked_by(later, {"features": WAITING}) == ("features", WAITING)
    assert plan.blocked_by(features, {"quality": FAILED, "ca": MANUAL}) == ("quality", SKIPPED)
    assert plan.exit_code({"listing": MANUAL, "bars": OK, "features": WAITING}) == 0
    assert plan.exit_code({"listing": MANUAL, "bars": FAILED}) == 1


def test_only_a_stale_ok_step_reads_a_manual_ones_stored_data() -> None:
    """The 5-minute top-up reads the members as last loaded: Kotak reaches back 30 days, so it
    must not wait on a NIFTY 500 list handed over late. Harm: a month of 5-minute bars lost."""
    steps = {s.name: s for s in plan.STEPS}
    assert plan.blocked_by(steps["bars5m"], {"bars": OK, "membership": MANUAL}) is None
    assert plan.blocked_by(steps["bars5m"], {"bars": OK, "membership": FAILED}) is not None
    assert plan.blocked_by(steps["features"], {"quality": OK, "ca": MANUAL}) == ("ca", WAITING)
    assert plan.blocked_by(steps["quality"], {"ingest": OK, "tri": MANUAL}) is None


def test_ca_runs_only_the_sessions_up_to_the_first_without_its_pr_archive() -> None:
    """A later session's `ca.adjust` waits for an earlier one's file. Harm: a session run before
    the one before it would store its dividends while the earlier night's are missing."""
    from app.hat.data.update.steps import ready_prefix

    assert ready_prefix([MON, TUE, WED], {MON, WED}) == [MON]
    assert ready_prefix([MON, TUE], {MON, TUE}) == [MON, TUE]
    assert ready_prefix([MON, TUE], {TUE}) == []


def test_the_manual_detail_names_each_address_file_and_the_import_command() -> None:
    """The owner reads what to download, the name to save it under and the command that takes
    it; a niftyindices series is a POST, so its curl line carries the request body."""
    from app.hat.data.update import hand

    pr = hand.pr_archive(date(2026, 10, 30))
    assert pr.name == "PR301026.zip"
    assert pr.url == "https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR301026.zip"
    text = hand.manual("ca", [pr], "note")
    assert text.startswith("note: download https://nsearchives")
    assert text.endswith("scripts/hat_data_update.sh --only ca --file PR301026.zip")
    tri = hand.nifty500_tri(date(2026, 1, 1), date(2026, 10, 30))
    assert tri.name == "NIFTY_500_TRI_20260101_20261030.json"
    words = shlex.split(tri.how())  # the shell gives curl the body whole
    assert words[:4] == ["curl", "-s", "-X", "POST"]
    assert words[words.index("--data") + 1] == tri.body
    assert "'name':'NIFTY 500'" in tri.body
    assert "01-Jan-2026" in tri.body
    assert words[-2:] == ["-o", "NIFTY_500_TRI_20260101_20261030.json"]
    both = hand.manual("ca", [pr, hand.SYMBOL_CHANGES])
    assert both.endswith("--only ca --file PR301026.zip symbolchange.csv")


def test_a_series_file_name_gives_its_last_day() -> None:
    from app.hat.data.update import hand

    end = hand.series_end("NIFTY_50_TRI_20260101_20261030.json", "NIFTY_50_TRI")
    assert end == date(2026, 10, 30)
    assert hand.series_end("NIFTY_500_TRI_20260101_20261030.json", "NIFTY_50_TRI") is None
    assert hand.series_end("NIFTY_50_TRI_20260101_2026.json", "NIFTY_50_TRI") is None
    assert hand.series_end("NIFTY_50_TRI_20260101_20261332.json", "NIFTY_50_TRI") is None


def _series(folder: str, *days: date) -> str:
    """A niftyindices answer of the overlay series `folder` holding `days`."""
    import json

    from app.hat.data.update.steps import OVERLAY_KEYS, OVERLAY_SERIES

    date_key, value_key = OVERLAY_KEYS[OVERLAY_SERIES[folder][1]]
    return json.dumps([{date_key: f"{d:%d %b %Y}", value_key: "1,234.5"} for d in days])


def test_the_overlay_is_short_until_every_series_holds_each_year_and_the_session(
    tmp_path: Path,
) -> None:
    """Coverage is read from the files' rows, not their names, for every year from the overlay's
    start. Harm: an overlay built from series ending before the session, from an error page
    saved under a full-year name, or from a fresh folder holding this year only, stored with
    gaps that read as data."""
    from app.hat.data.update.steps import OVERLAY_SERIES, OVERLAY_START, overlay_short

    for folder in OVERLAY_SERIES:
        (tmp_path / "niftyindices" / folder).mkdir(parents=True)
        for year in range(OVERLAY_START.year, FRI.year):
            (tmp_path / "niftyindices" / folder / f"{year}0101_{year}1231.json").write_text(
                _series(folder, date(year, 6, 1))
            )
        (tmp_path / "niftyindices" / folder / "20260101_20261030.json").write_text(
            _series(folder, THU, FRI)
        )
    assert overlay_short(tmp_path, FRI) == {}
    rate = tmp_path / "niftyindices" / "NIFTY_1D_RATE_INDEX" / "20260101_20261030.json"
    rate.write_text(_series("NIFTY_1D_RATE_INDEX", WED, THU))  # named to FRI, rows to THU
    tri = tmp_path / "niftyindices" / "NIFTY_50_TRI" / "20180101_20181231.json"
    tri.write_text("<html>Access Denied</html>")
    (tmp_path / "niftyindices" / "NIFTY_500_TRI" / "20150101_20151231.json").unlink()
    assert overlay_short(tmp_path, FRI) == {
        "NIFTY_50_TRI": [2018],
        "NIFTY_500_TRI": [2015],
        "NIFTY_1D_RATE_INDEX": [2026],
    }


def test_the_overlay_hand_request_names_each_missing_year() -> None:
    from app.hat.data.update.steps import overlay_manual

    text = overlay_manual({"NIFTY_500_TRI": [2015, 2026]}, FRI)
    assert "-o NIFTY_500_TRI_20150101_20151231.json" in text
    assert "-o NIFTY_500_TRI_20260101_20261030.json" in text
    given = "NIFTY_500_TRI_20150101_20151231.json NIFTY_500_TRI_20260101_20261030.json"
    assert text.endswith(f"--only etf --file {given}")


def test_ca_retires_only_the_hand_files_a_session_read(tmp_path: Path) -> None:
    """Harm: a change list handed over without its pair, or on a night no session was ready,
    moved to `used/` unread, so the ISIN lineage never gets it."""
    from app.hat.data.update.steps import ca_used

    pr, sym, name = (tmp_path / n for n in ("PR301026.zip", "symbolchange.csv", "namechange.csv"))
    assert ca_used([sym, name], [pr]) == [sym, name, pr]
    assert ca_used([sym, name], []) == []  # no session ran
    assert ca_used([sym, None], [pr]) == [pr]  # one list alone is not read
    assert ca_used([None, None], [pr]) == [pr]


def test_a_handed_overlay_file_is_used_only_when_valid_and_replaces_only_a_broken_one(
    tmp_path: Path,
) -> None:
    """Harm: an error page copied in for good (a raw file is kept once), or a valid kept series
    overwritten by a later hand file."""
    from app.hat.data.update import hand
    from app.hat.data.update.steps import Context, _overlay_hand_files

    inbox, work = tmp_path / hand.INBOX, tmp_path / "work"
    inbox.mkdir(parents=True)
    kept = work / "niftyindices" / "NIFTY_50_TRI" / "20260101_20261030.json"
    kept.parent.mkdir(parents=True)
    kept.write_text("<html>Access Denied</html>")
    (inbox / "NIFTY_50_TRI_20260101_20261030.json").write_text(_series("NIFTY_50_TRI", FRI))
    (inbox / "NIFTY_500_TRI_20260101_20261030.json").write_text("not json")
    ctx = Context(tmp_path, WEEK, FRI, FRI, tmp_path / "log", False, None)
    _overlay_hand_files(ctx, work)
    assert kept.read_text() == _series("NIFTY_50_TRI", FRI)  # the broken file replaced
    assert not (work / "niftyindices" / "NIFTY_500_TRI").exists()  # the bad hand file unused
    (inbox / "NIFTY_50_TRI_20260101_20261030.json").write_text(_series("NIFTY_50_TRI", THU))
    _overlay_hand_files(ctx, work)
    assert kept.read_text() == _series("NIFTY_50_TRI", FRI)  # a valid kept file stays


def test_a_handed_snapshot_keeps_its_download_day_and_is_never_loaded_before_a_change(
    tmp_path: Path,
) -> None:
    """The NIFTY 500 list is a snapshot of the day it was downloaded. Harm: a list loaded as of a
    later day than it shows (members wrong for the days between), or an older list undoing a
    stored entry or exit."""
    from app.hat.data.update import hand

    given = tmp_path / "ind_nifty500list.csv"
    given.write_text("x")
    (copy,) = hand.take(tmp_path, [given], WED)
    assert hand.as_of(copy) == WED
    hand.take(tmp_path, [given])  # handed again without a day: the old day goes with the file
    assert hand.as_of(copy) is None
    hand.take(tmp_path, [given], THU)
    hand.retire(tmp_path, [copy], FRI)
    assert hand.as_of(tmp_path / hand.USED / FRI.isoformat() / copy.name) == THU
    assert not copy.with_name(copy.name + hand.AS_OF_SUFFIX).exists()
    assert hand.as_of_refused(THU, FRI, WED) is None
    assert hand.as_of_refused(THU, FRI, THU) is None
    assert hand.as_of_refused(TUE, FRI, WED) == (
        "as_of_before_change: 2026-10-27 is before the stored change of 2026-10-28"
    )
    assert hand.as_of_refused(date(2026, 10, 31), FRI, None) == "as_of_in_future: 2026-10-31"
    text = hand.manual("membership", [hand.NIFTY500_LIST], dated=True)
    assert "--file ind_nifty500list.csv --as-of <the day you downloaded it" in text


def test_the_tri_step_asks_for_every_day_since_the_last_stored_one() -> None:
    """Harm: a TRI day missing for longer than the nightly look-back (a file handed over late)
    no longer flagged, so never filled."""
    from datetime import timedelta

    from app.hat.data.index_history import TRI_LOOKBACK, tri_since

    assert tri_since(date(2026, 8, 3), FRI) == date(2026, 8, 4)
    assert tri_since(THU, FRI) == FRI - TRI_LOOKBACK  # never less than the look-back
    assert tri_since(None, FRI) == FRI - TRI_LOOKBACK
    assert tri_since(FRI - TRI_LOOKBACK - timedelta(days=1), FRI) == FRI - TRI_LOOKBACK
