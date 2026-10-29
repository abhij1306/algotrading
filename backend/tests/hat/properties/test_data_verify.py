"""The data verification's pure seams (`app.hat.data.verify`): the comparators, parsers and
classifiers of each check on hand-built rows, and the report's window status the data audit
reads."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from app.hat.data import audit, verify
from app.hat.data.bars_basis import DailyRange
from app.hat.data.verify import (
    adjustments,
    bars5m,
    filings,
    membership,
    rawbars,
    references,
    series,
    sessions,
    snapshot,
)
from app.hat.data.verify.adjustments import RefAction, StoredAction
from app.hat.data.verify.exceptions import Excepted, Exceptions
from app.hat.time import IST

D1, D2, D3 = date(2024, 3, 1), date(2024, 3, 4), date(2024, 3, 5)


# --- the report and the data audit --------------------------------------------------------------


SNAP = {"digest": "data-1", "parts": {}}
ALL_KINDS = dict.fromkeys(("split_bonus", "rights", "demerger", "dividend"), D1)


def _check(status: str, *findings: dict, coverage: tuple[date, date] | None = None) -> dict:
    result = {"status": status, "summary": {}, "findings": list(findings)}
    return result if coverage is None else verify.covered(result, *coverage)


def _report(statuses: dict[str, str], start: date = D1, end: date = D3) -> dict:
    checks = {
        k: _check(s, *([audit.finding("x", audit.FAIL, [])] if s == "fail" else []))
        for k, s in statuses.items()
    }
    return verify.report_doc(checks, start, end, datetime(2026, 10, 29, tzinfo=UTC), SNAP)


def _status(
    doc: dict | None, start: date = D1, end: date = D3, current: str = "data-1", **kw: object
) -> dict:
    found = [] if doc is None else [(doc, None)]
    args = {"used_5m": False} | kw
    return verify.window_status(found, start, end, lambda _: current, **args)  # type: ignore[arg-type]


def test_the_digest_ignores_the_creation_time() -> None:
    a = _report({"D1": "pass"})
    b = verify.report_doc({"D1": _check("pass")}, D1, D3, datetime(2027, 1, 1, tzinfo=UTC), SNAP)
    assert a["digest"] == b["digest"]
    assert a["created"] != b["created"]


def test_a_failed_blocking_check_leaves_the_window_unverified() -> None:
    every = dict.fromkeys(verify.CHECKS, "pass")
    assert _status(_report(every), used_5m=True)["verified"]
    failed = _status(_report(every | {"D2": "fail"}))
    assert (failed["verified"], failed["failed"]) == (False, ["D2"])


def test_d4_blocks_only_a_run_that_reads_5_minute_bars() -> None:
    doc = _report(dict.fromkeys(verify.CHECKS, "pass") | {"D4": "fail"})
    assert _status(doc)["verified"]
    assert _status(doc, used_5m=True)["failed"] == ["D4"]


def test_a_blocking_check_not_run_leaves_the_window_unverified() -> None:
    status = _status(_report({"D1": "pass"}))
    assert not status["verified"]
    assert "D2" in status["not_run"]


def test_a_warning_never_stands_for_a_missing_reference() -> None:
    # every check's "no reference" finding fails, so a report without its references never
    # verifies a window (blocking item 1)
    every = dict.fromkeys(verify.CHECKS, "pass")
    missing = _check("fail", audit.finding("notices_missing", audit.FAIL, []))
    status = _status(_report(every) | {"checks": _report(every)["checks"] | {"D3": missing}})
    assert status["failed"] == ["D3"]


def test_each_check_is_judged_on_the_runs_own_window() -> None:
    every = {k: _check("pass") for k in verify.CHECKS}
    early = date(2012, 1, 2)
    gap = verify.dated(audit.finding("sessions_without_membership", audit.FAIL, []), [early])
    late_only = _check("pass", coverage=(D2, D3))
    doc = verify.report_doc(
        every | {"D3": _check("fail", gap), "D7": late_only},
        early,
        D3,
        datetime(2026, 10, 29, tzinfo=UTC),
        SNAP,
    )
    # a 2012 run: no membership then fails D3; D7 does not cover it
    old = _status(doc, early, early)
    assert (old["failed"], old["not_run"]) == (["D3"], ["D7"])
    # a 2024 run: the 2012 gap does not concern it, and D7 covers it
    assert _status(doc, D2, D3)["verified"]
    spanned = verify.spanning(audit.finding("gap", audit.FAIL, []), D1, D1)
    assert verify.failing_in(spanned, D1, D3)
    assert not verify.failing_in(spanned, D2, D3)
    assert verify.failing_in(audit.finding("whole", audit.FAIL, []), D2, D3)
    assert not verify.failing_in(audit.finding("look", audit.WARN, []), D1, D3)


def test_a_report_on_changed_data_does_not_verify() -> None:
    # blocking item 5: the report records the snapshot it judged
    doc = _report(dict.fromkeys(verify.CHECKS, "pass"))
    assert _status(doc)["verified"]
    stale = _status(doc, current="data-2")
    assert not stale["verified"]
    assert stale["stale"] == sorted(verify.BLOCKING)
    found = verify.window_status([(doc, Path("v.json"))], D1, D3, lambda _: "data-2", used_5m=False)
    assert audit.check_verify(found)["findings"][0]["code"] == "verify_report_stale"


def test_a_narrow_run_never_hides_a_wider_reports_checks() -> None:
    # second review item 1: a newer D1-only run covers every window; the other checks are judged
    # by the older full report, and a check is judged by the newest fresh report that ran it
    full = _report(dict.fromkeys(verify.CHECKS, "pass"))
    narrow = verify.report_doc(
        {"D1": _check("pass")}, verify.FIRST_DAY, D3, datetime(2026, 10, 31, tzinfo=UTC), SNAP
    )
    both = [(narrow, Path("narrow.json")), (full, Path("full.json"))]
    status = verify.window_status(both, D1, D3, lambda _: "data-1", used_5m=False)
    assert status["verified"]
    assert status["reports"]["D1"] == "narrow.json"
    assert status["reports"]["D2"] == "full.json"
    # a newer failing D2 run decides D2, even with the older passing one
    failing = verify.report_doc(
        {"D2": _check("fail", audit.finding("x", audit.FAIL, []))},
        verify.FIRST_DAY,
        D3,
        datetime(2026, 11, 1, tzinfo=UTC),
        SNAP,
    )
    status = verify.window_status(
        [(failing, None), *both], D1, D3, lambda _: "data-1", used_5m=False
    )
    assert status["failed"] == ["D2"]
    # a newer report on changed data is passed over for an older one on the data now
    fresh = verify.report_doc(
        {"D2": _check("pass")},
        verify.FIRST_DAY,
        D3,
        datetime(2026, 10, 30, tzinfo=UTC),
        {"digest": "data-old"},
    )
    pick = verify.pick_reports([(fresh, None), (full, None)], "D2", lambda d: "data-1", D1, D3)
    assert pick == (full, None)
    assert verify.pick_reports([(fresh, None)], "D2", lambda d: "data-1", D1, D3) == "stale"


def test_a_run_is_judged_from_the_first_session_it_reads() -> None:
    # second review item 2: the market loads LOOKBACK_SESSIONS before the period; a failure in
    # the lookback leaves the run unverified
    early = date(2014, 6, 2)
    assert verify.read_window({"store_first": early.isoformat()}, D2, D3) == (early, D3)
    assert verify.read_window({}, D2, D3) == (D2, D3)
    gap = verify.dated(audit.finding("sessions_without_membership", audit.FAIL, []), [early])
    every = {k: _check("pass") for k in verify.CHECKS}
    doc = verify.report_doc(
        every | {"D3": _check("fail", gap)}, early, D3, datetime(2026, 10, 29, tzinfo=UTC), SNAP
    )
    assert _status(doc, D2, D3)["verified"]
    assert _status(doc, *verify.read_window({"store_first": early.isoformat()}, D2, D3))[
        "failed"
    ] == ["D3"]


def test_the_snapshot_reads_each_store_files_day() -> None:
    assert snapshot.file_day("bars_5m/date=2024-03-01/bars.parquet") == D1
    assert snapshot.file_day("raw/cm01MAR2024bhav.csv.zip") == D1
    assert snapshot.file_day("raw/ind_close_all_01032024.csv") == D1
    assert snapshot.file_day("raw/BhavCopy_NSE_CM_0_0_0_20240301_F_0000.csv.zip") == D1
    assert snapshot.file_day("raw/reference/nse/symbolchange.csv") is None
    a = snapshot.files_digest([("p", "h1")])
    assert a == snapshot.files_digest([("p", "h1")])
    assert a != snapshot.files_digest([("p", "h2")])  # rewritten in place


def test_the_snapshot_trusts_the_manifest_only_for_files_unchanged_since() -> None:
    # second review item 6: content, never size and time; the manifest's sha256 spares reading
    # a file only when the file has not changed since the manifest was written
    row = {"bytes": "10", "sha256": "recorded"}

    def read() -> str:
        return "read"

    assert snapshot.file_entry(10, 100, row, 200, read) == "recorded"
    assert snapshot.file_entry(10, 300, row, 200, read) == "read"  # changed after the record
    assert snapshot.file_entry(11, 100, row, 200, read) == "read"  # another size
    assert snapshot.file_entry(10, 100, None, 200, read) == "read"  # not recorded
    windows = {"ABC": (date(2025, 10, 22), date(2026, 10, 21))}
    cut = snapshot.windows_digest(windows, D1, D3)
    assert cut == snapshot.windows_digest(windows | {"NEW": (date(2026, 10, 22), D3)}, D1, D3)
    assert cut != snapshot.windows_digest({"ABC": (D1, D3)}, D1, D3)


def test_a_report_on_data_that_moved_during_the_run_verifies_nothing() -> None:
    results = {"D1": _check("pass")}
    same = verify.changed_during(results, SNAP, SNAP)
    assert same == results
    moved = verify.changed_during(
        results, SNAP, {"digest": "data-2", "parts": {"files:bars_daily_raw": "x"}}
    )
    assert moved["D1"]["status"] == audit.FAIL
    assert verify.check_status(moved["D1"], D1, D3) == "failed"


def test_the_newest_report_covering_the_window_is_read(tmp_path: Path) -> None:
    old = _report(dict.fromkeys(verify.CHECKS, "fail"))
    new = verify.report_doc(
        {k: _check("pass") for k in verify.CHECKS},
        D1,
        D3,
        datetime(2026, 10, 30, tzinfo=UTC),
        SNAP,
    )
    narrow = verify.report_doc({}, D2, D3, datetime(2026, 10, 31, tzinfo=UTC), SNAP)
    for name, doc in (("a", old), ("b", new), ("c", narrow)):
        verify.write_report(tmp_path, Path(f"verify-{name}"), doc, "")
    found = verify.covering_reports(tmp_path, D1, D3)
    assert [d["digest"] for d, _ in found] == [new["digest"], old["digest"]]
    assert verify.covering_reports(tmp_path, D1, D3 + timedelta(days=1)) == []


def test_a_narrow_run_never_replaces_the_full_report(tmp_path: Path) -> None:
    full = verify.report_folder(tmp_path, D1, verify.FIRST_DAY, D3, sorted(verify.CHECKS))
    narrow = verify.report_folder(tmp_path, D1, D2, D3, ["D2"])
    assert full.name == "verify-2024-03-01"
    assert narrow.name == "verify-2024-03-01-D2-20240304-20240305"


def test_a_report_folder_outside_the_reports_folder_is_refused(tmp_path: Path) -> None:
    inside = verify.report_target(tmp_path, Path("verify-mine"))
    assert inside == (verify.reports_dir(tmp_path) / "verify-mine").resolve()
    with pytest.raises(ValueError, match="report_folder_not_verify"):
        verify.report_target(tmp_path, Path("mine"))  # written, but never read
    full = verify.report_folder(tmp_path, D1, D2, D3, ["D2"])
    assert verify.report_target(tmp_path, full) == full.resolve()
    for outside in (Path("../x"), tmp_path.parent / "x", Path(".")):
        with pytest.raises(ValueError, match="report_folder_outside_audits"):
            verify.report_target(tmp_path, outside)


def test_the_audit_warns_an_unverified_window_and_never_fails_it() -> None:
    missing = audit.check_verify(_status(None))
    assert missing["status"] == audit.WARN
    assert missing["findings"][0]["code"] == "verify_report_missing"
    failed = audit.check_verify(
        verify.window_status(
            [(_report({"D1": "fail"}), Path("v.json"))], D1, D3, lambda _: "data-1", used_5m=False
        )
    )
    codes = {f["code"] for f in failed["findings"]}
    assert codes == {"verify_blocking_failed", "verify_blocking_not_run"}
    assert failed["status"] == audit.WARN
    assert not failed["summary"]["verified"]


def test_run_json_carries_the_verification() -> None:
    doc = {"checks": [{"name": "verify", "summary": {"verified": True, "digest": "abc"}}]}
    counts = audit.run_counts(doc)
    assert (counts["verified"], counts["verify_report"]) == (True, "abc")
    assert audit.run_counts({"checks": []})["verified"] is None


def test_a_new_report_changes_the_audit_key() -> None:
    key = audit.cache_key("s", D1, D3, used_5m=False, golden_digest=None, verify_digest="a")
    assert key != audit.cache_key("s", D1, D3, used_5m=False, golden_digest=None)


def test_d3_keeps_todays_list_under_today_only_for_a_run_to_the_last_session() -> None:
    # second review item 5: a past window never stores today's list under its own end date
    today, last = date(2026, 10, 29), date(2026, 10, 28)
    assert membership.live_list_name(today, last, last) == "20261029000000_nsearchives-live.csv"
    assert membership.live_list_name(today, date(2024, 12, 31), last) is None
    assert membership.live_list_name(today, last, None) is None


def test_probe_answers_merge_across_the_runs_files() -> None:
    # second review item 7: each run writes its new answers once; nothing is rewritten
    first = json.dumps({"2024-03-02": references.NOT_FOUND})
    second = json.dumps({"2024-03-09": references.FOUND, "x": "maybe"})
    assert references.merge_probes([first, second, "not json"]) == {
        "2024-03-02": references.NOT_FOUND,
        "2024-03-09": references.FOUND,
    }


# --- D1 -----------------------------------------------------------------------------------------


def _source(isin: str, series_: str = "EQ", close: str = "10", day: date = D1) -> rawbars.SourceRow:
    p = Decimal(close)
    return rawbars.SourceRow(day, isin, series_, "ABC", p, p, p, p, p, Decimal(5), Decimal(50))


def _stored(isin: str, close: str = "10") -> dict:
    p = Decimal(close)
    return {
        "isin": isin,
        "series": "EQ",
        "symbol": "ABC",
        "open": p,
        "high": p,
        "low": p,
        "close": p,
        "prev_close": p,
        "volume": 5,
        "traded_value": Decimal(50),
    }


def test_d1_equal_rows_match_and_a_wrong_close_is_named() -> None:
    result = rawbars.compare_day(
        D1,
        [_stored("I1"), _stored("I2") | {"close": Decimal("10.5")}],
        [_source("I1"), _source("I2")],
        {"I1", "I2"},
    )
    assert result.matched == 1
    assert result.counts["value_mismatch"] == 1
    assert result.examples["value_mismatch"][0]["field"] == "close"


def test_d1_file_rows_not_stored_are_sorted_by_why() -> None:
    source = [_source("I1"), _source("I2"), _source("I3"), _source("I1", "BE")]
    result = rawbars.compare_day(D1, [_stored("I1")], source, {"I1", "I2"})
    assert result.counts[rawbars.NOT_STORED] == 1  # I2: an instrument HAT has
    assert result.counts[rawbars.NO_INSTRUMENT] == 1  # I3
    assert result.counts[rawbars.OTHER_SERIES] == 1  # I1 BE beside the stored EQ
    assert result.counts["stored_not_in_source"] == 0


def test_d1_a_file_dated_off_its_day_fails_even_when_it_matches() -> None:
    # blocking item 2: a stale file under day X that equals the store is no match
    stale = rawbars.compare_day(D2, [_stored("I1")], [_source("I1", day=D1)], {"I1"})
    assert stale.matched == 0
    assert stale.counts[rawbars.OFF_DATE] == 1
    assert stale.counts["stored_not_in_source"] == 1
    empty = rawbars.compare_day(D2, [], [_source("I1", "N1")], {"I1"})
    assert empty.counts[rawbars.EMPTY] == 1
    assert rawbars._file_date("13-Jul-20") == date(2020, 7, 13)  # NSE's file of that day
    result = rawbars.summarise([(D2, stale)], [], [])
    assert result["status"] == audit.FAIL
    off = next(f for f in result["findings"] if f["code"] == rawbars.OFF_DATE)
    assert off["days"] == [D2.isoformat()]


# --- D2 -----------------------------------------------------------------------------------------


def test_d2_parses_the_reference_terms_on_its_own() -> None:
    assert adjustments.bonus_factor("1:1") == Fraction(1, 2)
    assert adjustments.bonus_factor("2:15") == Fraction(15, 17)
    assert adjustments.bonus_factor("0:1") is None
    assert adjustments.split_factor("Face Value Split From Rs.10/- To Rs.2/-") == Fraction(1, 5)
    assert adjustments.split_factor("", "10->1") == Fraction(1, 10)
    assert adjustments.split_factor("Bonus 1:1") is None


def test_d2_reads_every_dividend_of_a_purpose_text() -> None:
    cash = adjustments.dividend_cash
    assert cash("Annual General Meeting / Dividend - Final Rs 22 + Special Rs 10") == 32
    assert cash("Interim Dividend - Rs 5 Per Share / Special Dividend - Rs 2") == 7
    assert cash("Dividend - Rs 106 Per Share Special Dividend 243 Per Share") == 349
    assert cash("Dividend Rs. 2.50/- Per Equity Share Of Rs.10/- Each") == Decimal("2.50")
    assert cash(
        "Dividend-Rs.8.50 Per Share (Including Special Dividend Of Rs.2/- Per Share)"
    ) == Decimal("8.50")
    assert cash("Interim Div - Rs 6/- Per Share + Face Value Split From Rs 10/- To Rs 2/-") == 6
    assert cash("Dividend - Re 0..30 Per Share") == Decimal("0.30")
    assert cash("Face Value Split (Sub-Division) - From Rs 2/- Per Share To Re 1/-") is None
    assert not adjustments.names_dividend("Face Value Split (Sub-Division)")


def test_d2_rights_terp_on_the_issue_days_face_value() -> None:
    assert adjustments.rights_factor(1, 1, Decimal(50), Decimal(100)) == Fraction(3, 4)
    assert adjustments.rights_factor(1, 1, Decimal(120), Decimal(100)) == 1
    # today's face value 1 after a 10 -> 1 split in 2020: the 2018 issue was at 10 + 40
    fv = adjustments.issue_face_value(
        Decimal(1), date(2018, 1, 1), [(date(2020, 1, 1), Fraction(1, 10))]
    )
    assert fv == 10
    assert adjustments.issue_price("Rights 1:3 @ Premium Rs 40/-", "", fv) == 50
    assert adjustments.issue_price("Rights 2:7", "", Decimal(10)) is None
    assert adjustments.issue_price("Rights 1:2 @ Rs 150 Per Share", "", Decimal(10)) == 150
    stored = adjustments.rights_factor(1, 3, Decimal(50), Decimal(100))
    assert adjustments.judge_rights(stored, 1, 3, Decimal(50), Decimal(100))[0] == "equal"
    assert adjustments.judge_rights(stored, 1, 3, Decimal(41), Decimal(100))[0] == "factor_mismatch"


def test_d2_rights_tolerance_is_on_the_discount() -> None:
    # 1:100 at a 40% discount: factor 0.99604; stored 1.0 is within 0.5% of the factor but
    # misses the whole discount
    code, expected = adjustments.judge_rights(Fraction(1), 1, 100, Decimal(60), Decimal(100))
    assert code == "factor_mismatch"
    assert expected < 1
    near = expected - (1 - expected) / 100  # 1% off the discount
    assert adjustments.judge_rights(near, 1, 100, Decimal(60), Decimal(100))[0] == "equal"
    assert adjustments.discount_close(Fraction(1), Fraction(1))


def test_d2_two_distinct_dividends_need_both_stored() -> None:
    keys = {"a": frozenset({"ABC"})}
    two = RefAction(
        "dividend", D2, frozenset({"ABC"}), cash=Decimal(15), amounts=(Decimal(5), Decimal(10))
    )
    stored = [_stored_action("a", "dividend", "1", "5")]
    summary, _ = adjustments.compare_factors([two], stored, keys, {}, [], ALL_KINDS)
    assert summary == {"dividend:cash_mismatch": 1}
    repeated = RefAction(
        "dividend", D2, frozenset({"ABC"}), cash=Decimal(10), amounts=(Decimal(5), Decimal(5))
    )
    summary, _ = adjustments.compare_factors([repeated], stored, keys, {}, [], ALL_KINDS)
    assert summary == {"dividend:equal_one_row": 1}


def test_d2_a_reference_action_of_no_instrument_is_a_counted_finding() -> None:
    ref = RefAction("split_bonus", D2, frozenset({"NOHAT"}), Fraction(1, 2))
    _, findings = adjustments.compare_factors([ref], [], {}, {}, [], ALL_KINDS)
    assert [(f["code"], f["severity"], f["count"]) for f in findings] == [
        ("split_bonus_no_instrument", audit.WARN, 1)
    ]


def test_d2_stored_actions_before_their_references_start_are_not_run() -> None:
    # second review item 4: a reference pull starting in 2014 leaves 2011-13 not run, never
    # verified; a stored action before its kind's start is not judged
    refs = [
        RefAction("split_bonus", date(2014, 3, 12), frozenset({"ABC"}), Fraction(1, 2)),
        RefAction("rights", date(2015, 6, 1), frozenset({"ABC"}), Fraction(9, 10)),
    ]
    starts = adjustments.reference_starts(refs, date(2011, 6, 22))
    assert starts == {"split_bonus": date(2014, 3, 12), "rights": date(2015, 6, 1)}
    assert adjustments.factor_coverage(starts, D3) is None  # no demerger or dividend reference
    every = dict.fromkeys(adjustments.KINDS, date(2014, 3, 1)) | starts
    span = adjustments.factor_coverage(every, D3)
    assert span == (date(2015, 6, 1), D3)
    result = verify.covered(_check("pass"), *span)
    assert verify.check_status(result, date(2012, 1, 2), D3) == "not_run"
    assert verify.check_status(result, D1, D3) == "verified"
    early = StoredAction("a", date(2012, 5, 2), "split", Fraction(1, 2), None, False, "mcp")
    summary, _ = adjustments.compare_factors([], [early], {"a": frozenset({"ABC"})}, {}, [], every)
    assert summary == {}


def _stored_action(
    instrument: str, kind: str, factor: str, cash: str | None = None
) -> StoredAction:
    return StoredAction(
        instrument,
        D2,
        kind,
        Fraction(factor),
        None if cash is None else Decimal(cash),
        False,
        "mcp",
    )


def test_d2_a_days_split_and_bonus_are_judged_as_one_product_once_per_chain() -> None:
    legs = [
        RefAction("split_bonus", D2, frozenset({"ABC"}), Fraction(1, 2), text="split"),
        RefAction("split_bonus", D2, frozenset({"ABC"}), Fraction(1, 2), text="bonus"),
    ]
    ref = adjustments.combine_legs(legs)
    keys = {"old": frozenset({"ABC"}), "new": frozenset({"ABC", "INE1"})}
    stored = [  # the same legs on both ISINs of the chain: applied once, not squared
        _stored_action(i, t, "0.5") for i in ("old", "new") for t in ("split", "bonus")
    ]
    summary, findings = adjustments.compare_factors([ref], stored, keys, {}, [], ALL_KINDS)
    assert summary == {"split_bonus:equal": 1}
    assert findings == []


def test_d2_a_wrong_or_missing_action_fails_and_an_owner_deletion_warns() -> None:
    keys = {"a": frozenset({"ABC"}), "b": frozenset({"XYZ"})}
    refs = [
        RefAction("split_bonus", D2, frozenset({"ABC"}), Fraction(1, 2)),
        RefAction("split_bonus", D2, frozenset({"XYZ"}), Fraction(1, 2)),
        RefAction("dividend", D2, frozenset({"XYZ"}), cash=Decimal(3), amounts=(Decimal(3),)),
    ]
    stored = [_stored_action("a", "bonus", "0.9705")]
    closes = {("b", D2): Decimal(50)}  # XYZ traded before the ex-date: the dividend applies
    summary, findings = adjustments.compare_factors(
        refs, stored, keys, closes, [("b", D2, "split")], ALL_KINDS
    )
    assert summary["split_bonus:factor_mismatch"] == 1
    assert summary["split_bonus:deleted_by_owner"] == 1
    levels = {f["code"]: f["severity"] for f in findings}
    assert levels["split_bonus_factor_mismatch"] == audit.FAIL
    assert levels["split_bonus_deleted_by_owner"] == audit.WARN
    assert levels["dividend_not_stored"] == audit.FAIL
    mismatch = next(f for f in findings if f["code"] == "split_bonus_factor_mismatch")
    assert mismatch["days"] == [D2.isoformat()]


def test_d2_moves_are_classed_by_what_explains_them() -> None:
    move = {"ex_date": True, "move_pct": 20.02, "raw_move_pct": -85.0}
    assert adjustments.classify_move(move, False, 100.0) == "ex_date_circuit"
    assert adjustments.classify_move(move | {"move_pct": 51.0}, False, 100.0) == "ex_date_jump"
    plain = {"ex_date": False, "move_pct": -50.0, "raw_move_pct": -50.0}
    assert adjustments.classify_move(plain, False, 0.02) == "penny"
    assert adjustments.classify_move(plain, False, 100.0) == "action_shaped"
    crash = plain | {"move_pct": -30.0, "raw_move_pct": -30.0}
    assert adjustments.classify_move(crash, True, 100.0) == "results"
    assert adjustments.classify_move(crash, False, 100.0) == "unexplained"
    said = [filings.Announcement(datetime(2024, 3, 2, 18, tzinfo=IST), None)]
    assert adjustments.explained_by_results(D2, said)
    assert not adjustments.explained_by_results(D2 + timedelta(days=10), said)


def test_d2_second_source_returns_by_calendar_year() -> None:
    # Yahoo's monthly bars start at local midnight: 1 Jan 2023 IST is 31 Dec 2022 UTC
    months = [datetime(2021, 12, 1), datetime(2022, 12, 1), datetime(2023, 1, 1)]
    stamps = [int(m.replace(tzinfo=IST).timestamp()) for m in months]
    answer = {
        "meta": {"gmtoffset": 19800},
        "timestamp": stamps,
        "indicators": {"adjclose": [{"adjclose": [100, 110, 99]}]},
    }
    data = json.dumps({"chart": {"result": [answer]}}).encode()
    returns = adjustments.yahoo_year_returns(data)
    assert set(returns) == {2022, 2023}
    assert round(returns[2022], 6) == 10
    assert round(returns[2023], 6) == -10
    ours = adjustments.year_returns([(date(2021, 12, 31), 50.0), (date(2022, 12, 30), 56.0)])
    rows = adjustments.compare_returns(ours, {2022: 10.0}, 2022, 2022)
    assert rows == [{"year": 2022, "hat_pct": 12.0, "yahoo_pct": 10.0, "gap_pp": 2.0}]


# --- D3 -----------------------------------------------------------------------------------------


def test_d3_notice_overrides_apply_before_the_trace() -> None:
    parsed = [
        {
            "file": "n1",
            "effective": ["2024-03-04"],
            "sections": [{"excluded": [[1, "old"]], "included": [[1, "new"]]}],
        },
        {
            "file": "n2",
            "effective": ["2024-03-04"],
            "sections": [{"excluded": [[1, "X"]], "included": []}],
        },
    ]
    changes = membership.notice_changes(
        parsed, {"n2": {"skip": True}, "n1": {"date": "2024-03-05"}}
    )
    assert changes == [membership.NoticeChange("n1", (D3,), frozenset({"OLD"}), frozenset({"NEW"}))]
    members = [
        membership.Member("i-new", "INE2", "NEW", D3, None),
        membership.Member("i-old", "INE1", "OLD", D1, D3),
        membership.Member("i-ren", "INE3", "RENAMED", D3, None),
        membership.Member("i-odd", "INE4", "ODD", D3, None),
    ]
    trace = membership.trace_changes(
        members,
        [*changes, membership.NoticeChange("n3", (D3,), frozenset(), frozenset({"FORMER"}))],
        [D1, D2, D3],
        (D1, D3),
        (D1, D3),
        {"i-ren": {"FORMER"}},
    )
    assert len(trace.traced) == 3
    assert {t["notice"] for t in trace.traced} == {"n1", "n3"}
    assert [e["symbol"] for e in trace.entries] == ["ODD"]
    assert trace.exits == []
    assert trace.unapplied == []


def test_d3_a_notice_the_store_did_not_apply_fails() -> None:
    # blocking item 3: the other direction, notice -> store
    notice = membership.NoticeChange(
        "n1", (D3,), frozenset({"GONE"}), frozenset({"MISSING", "DUMMYHEG"})
    )
    members = [membership.Member("i-gone", "INE1", "GONE", D1, None)]
    trace = membership.trace_changes(members, [notice], [D1, D2, D3], (D1, D3), (D1, D3))
    assert {(u["symbol"], u["kind"]) for u in trace.unapplied} == {
        ("GONE", "exclusion"),
        ("MISSING", "inclusion"),
    }


def test_d3_an_isin_change_is_the_same_symbol_leaving_and_entering() -> None:
    members = [
        membership.Member("i-old", "INE1", "ABC", D1, D3),
        membership.Member("i-new", "INE9", "ABC", D3, None),
    ]
    trace = membership.trace_changes(members, [], [D1, D2, D3], (D1, D3), (D1, D3))
    assert {t.get("isin_change") for t in trace.traced} == {"INE1", "INE9"}
    assert trace.entries == trace.exits == []


def test_d3_reads_nse_symbol_changes_on_its_own() -> None:
    data = (
        b"NAME OF THE COMPANY,OLD SYMBOL,NEW SYMBOL,EFFECTIVE DATE\n"
        b" Abc Ltd,abc,ABCNEW,30-OCT-2019\n"
        b"Bad,X,,01-JAN-2020\n"
        b"Bad date,P,Q,someday\n"
    )
    renames, unread = references.read_symbol_changes(data)
    assert renames == [references.Rename("ABC", "ABCNEW", date(2019, 10, 30))]
    assert unread == 2


def test_d3_an_archived_list_difference_is_explained_or_named() -> None:
    listed = {"INE1": "A", "DUM545A01024": "DUMMYX", "INE9": "NOTHAT", "INE3": "C"}
    stored = {"INE1": "A", "INE2": "B"}
    explained, wrong = membership.compare_list(listed, stored, {"INE1", "INE2", "INE3"}, {"C"})
    assert {e["isin"] for e in explained} == {"DUM545A01024", "INE3"}
    # blocking item 3: a listed member HAT has no instrument for is survivorship, not explained
    assert {w["isin"] for w in wrong} == {"INE2", "INE9"}
    assert membership.capture_day("20181004105632_niftyindices.csv") == date(2018, 10, 4)


# --- D4 -----------------------------------------------------------------------------------------

DAILY = DailyRange(Decimal(100), Decimal(110), Decimal(95), Decimal(105))
TOL = Decimal("0.005")


def _day(o: float, h: float, lo: float, c: float) -> bars5m.DayBars:
    return bars5m.DayBars(o, h, lo, c, 75, "10:00", "11:00")


def test_d4_session_candles_aggregate_inside_the_session_only() -> None:
    at = datetime(2024, 3, 1, 9, 10, tzinfo=IST)
    candles = [
        bars5m.Candle(at, 1, 50, 1, 1),  # pre-open: not a session bar
        bars5m.Candle(at + timedelta(minutes=5), 100, 101, 99, 100.5),
        bars5m.Candle(at + timedelta(minutes=10), 100.5, 102, 100, 101),
    ]
    day = bars5m.session_days(candles)[D1]
    assert (day.open, day.high, day.low, day.close, day.candles) == (100, 102, 99, 101, 2)


def _src(hhmm: str, price: float, volume: int = 10) -> bars5m.SourceBar:
    h, m = map(int, hhmm.split(":"))
    at = datetime(2024, 3, 1, h, m, tzinfo=IST)
    return bars5m.SourceBar(at, price, price, price, price, volume)


def test_d4_reads_the_labelling_from_the_days_own_rows() -> None:
    end = [_src("09:15", 1, 0), _src("09:20", 10), _src("15:30", 11)]
    start = [_src("09:15", 10), _src("15:25", 11), _src("15:30", 11, 0)]
    assert bars5m.end_labelled(end)
    assert not bars5m.end_labelled(start)
    assert [c.start.time() for c in bars5m.candles_of(end)][1:] == [
        datetime(2024, 3, 1, 9, 15).time(),
        datetime(2024, 3, 1, 15, 25).time(),
    ]


def test_d4_stored_bars_match_the_source_bar_by_bar() -> None:
    # item 9: an end-labelled day stored at half the source's prices (a carried 1:1 bonus)
    t915, t920 = datetime(2024, 3, 1, 9, 15).time(), datetime(2024, 3, 1, 9, 20).time()
    source = [
        _src("09:15", 1, 0),
        _src("09:20", 100, 10),
        _src("09:25", 102, 20),
        _src("15:30", 101),
    ]
    stored = {t915: (50.0, 50.0, 50.0, 50.0, 20), t920: (51.0, 51.0, 51.0, 51.0, 40)}
    ok = bars5m.compare_bars(D1, stored, source)
    assert (ok.shift, ok.scale, ok.price, ok.volume, ok.stored_only) == (5, 0.5, 0, 0, 0)
    assert ok.source_only == 1  # the 15:25 bar (labelled 15:30) is not stored
    wrong = bars5m.compare_bars(D1, {**stored, t920: (51.5, 51.5, 51.5, 51.5, 40)}, source)
    assert wrong.price == 1


def test_d4_bars_stored_a_bar_off_fail_and_never_pair_up() -> None:
    # second review item 3: a day whose stored labels are all 5 minutes late pairs up perfectly
    # under the other alignment; it fails the rule and its bars differ
    prices = (100.0, 120.0, 90.0, 130.0, 95.0, 125.0)
    ends = [datetime(2024, 3, 1, 9, m).time() for m in range(20, 50, 5)]
    source = [_src("09:15", 1, 0), _src("15:30", 99)]
    source += [_src(f"{t:%H:%M}", p) for t, p in zip(ends, prices, strict=True)]
    starts = [(datetime.combine(D1, t) - timedelta(minutes=5)).time() for t in ends]
    on_rule = {t: (p, p, p, p, 10) for t, p in zip(starts, prices, strict=True)}
    late = {t: (p, p, p, p, 10) for t, p in zip(ends, prices, strict=True)}
    assert bars5m.compare_bars(D1, on_rule, source).rule
    off = bars5m.compare_bars(D1, late, source)
    assert off.shift == 5
    assert not off.rule
    assert off.price > 0


def test_d4_reads_the_fyers_download_windows() -> None:
    windows = bars5m.fyers_windows({"done": {"NSE:ABC-EQ": ["2025-10-22", "2026-10-21"], "X": 1}})
    assert windows == {"ABC": (date(2025, 10, 22), date(2026, 10, 21))}


def test_d4_refused_days_are_classed_in_order() -> None:
    def cls(bars: bars5m.DayBars | None, **kw: object) -> str:
        args = {"daily": DAILY, "stored_now": False, "factor": Decimal(1), "tolerance": TOL}
        return bars5m.classify(bars, **(args | kw))  # type: ignore[arg-type]

    fits = _day(100, 110, 95, 105)
    assert cls(fits, daily=None) == "no_daily_now"
    assert cls(fits, stored_now=True) == "stored_now"
    assert cls(None) == "no_source"
    assert cls(fits) == "fits_now"
    assert cls(fits, screened=True) == "screened_bucket"
    assert cls(_day(200, 220, 190, 210), factor=Decimal(2)) == "fits_converted"
    assert cls(_day(50, 55, 47.5, 52.5)) == "basis_factor"
    assert cls(_day(100, 115, 95, 105)) == "bad_source_exceeds"
    assert cls(_day(100, 104, 95, 104)) == "source_short_extreme"
    closing = DailyRange(Decimal(100), Decimal(110), Decimal(95), Decimal(110))
    assert cls(_day(100, 106, 95, 105), daily=closing) == "close_auction"
    opening = DailyRange(Decimal("109"), Decimal(110), Decimal(95), Decimal(100))
    assert cls(_day(108, 108, 95, 100), daily=opening) == "open_auction_near"
    assert set(bars5m.CAUSE) >= {"no_source", "basis_factor", "close_auction", "stored_now"}


def test_d4_gap_bias_compares_gap_days_with_the_rest() -> None:
    assert bars5m.gap_day(105, 100)
    assert not bars5m.gap_day(103, 100)
    assert not bars5m.gap_day(105, None)
    bias = bars5m.gap_bias([(True, 10, 5, 2), (False, 100, 90, 1)])
    assert bias == {
        "gap": {"days": 10, "stored_pct": 50.0, "refused_pct": 20.0},
        "other": {"days": 100, "stored_pct": 90.0, "refused_pct": 1.0},
    }


# --- D5 -----------------------------------------------------------------------------------------


def _filing(at: datetime, source: str = "nse") -> filings.Filing:
    return filings.Filing("ABC", "INE1", date(2023, 12, 31), "nse", at, source)


def test_d5_a_filing_public_before_nse_disseminated_it_fails() -> None:
    announced = datetime(2024, 2, 10, 15, 0, tzinfo=IST)
    found = [filings.Announcement(announced, announced + timedelta(minutes=3))]
    assert filings.judge(_filing(announced), found)[0] == "public_before_dissemination"
    assert filings.judge(_filing(announced + timedelta(minutes=3)), found)[0] is None
    assert filings.judge(_filing(announced + timedelta(days=3)), found)[0] == "late_vs_announcement"
    deadline = datetime(2024, 2, 9, 23, 59, tzinfo=IST)
    assert (
        filings.judge(_filing(deadline, "deadline"), found)[0]
        == "estimate_before_first_announcement"
    )
    assert filings.judge(_filing(announced), [])[0] == "unmatched"
    assert filings.SEVERITY["unmatched"] == audit.FAIL


def test_d5_an_unbroadcast_integrated_filing_at_its_creation_days_end_is_a_warning() -> None:
    created = datetime(2026, 1, 21, 16, 6, tzinfo=IST)
    day_end = datetime(2026, 1, 21, 23, 59, 59, tzinfo=IST)
    later = [filings.Announcement(day_end + timedelta(hours=10), None)]
    estimate = _filing(day_end, "xbrl_date")
    assert filings.judge(estimate, later, None, created)[0] == "xbrl_day_estimate"
    assert filings.SEVERITY["xbrl_day_estimate"] == audit.WARN
    # timed before that day's end, or without the creation time, it is judged by announcements
    early = _filing(created, "xbrl_date")
    assert filings.judge(early, later, None, created)[0] == "estimate_before_first_announcement"
    assert filings.judge(estimate, later)[0] == "estimate_before_first_announcement"


def test_d5_a_filing_is_judged_by_the_announcement_it_was_timed_by() -> None:
    # two announcements: one made earlier but disseminated late, one disseminated when stored
    first = datetime(2021, 10, 26, 16, 49, tzinfo=IST)
    stored = datetime(2021, 10, 26, 17, 26, tzinfo=IST)
    found = [
        filings.Announcement(first, first + timedelta(minutes=45)),
        filings.Announcement(stored - timedelta(minutes=1), stored),
    ]
    assert filings.judge(_filing(stored), found)[0] is None
    assert filings.judge(_filing(stored - timedelta(minutes=5)), found)[0] == (
        "public_before_dissemination"
    )
    # after the results were announced, a board meeting's outcome hours later does not make it
    # early
    results = datetime(2020, 7, 1, 1, 6, tzinfo=IST)
    found = [
        filings.Announcement(results, None),
        filings.Announcement(results + timedelta(hours=14), None, results=False),
    ]
    assert filings.judge(_filing(results + timedelta(hours=12)), found)[0] is None


def test_d5_a_filing_public_before_any_announcement_fails() -> None:
    # blocking item 4: public five days before the only announcement
    announced = datetime(2024, 2, 10, 15, 0, tzinfo=IST)
    found = [filings.Announcement(announced, announced)]
    early = _filing(announced - timedelta(days=5), "xbrl")
    assert filings.judge(early, found)[0] == "before_any_announcement"
    assert filings.SEVERITY["before_any_announcement"] == audit.FAIL
    # after the period's first announcement, nothing near: the results were already public
    later = [*found, filings.Announcement(announced + timedelta(days=15), None)]
    between = _filing(announced + timedelta(days=8), "xbrl")
    assert filings.judge(between, later)[0] == "between_announcements"
    assert filings.SEVERITY["between_announcements"] == audit.WARN


def test_d5_only_an_announcement_of_results_proves_they_were_public() -> None:
    # second review item 8: a clarification is no announcement; a board meeting's other outcome
    # is matched but never excuses a filing as between announcements
    kind = filings.announcement_kind
    assert kind("Clarification - Financial Results", "", "") is None
    assert kind("Reply to Clarification- Financial results", "", "") is None
    assert kind("Financial Result Updates", "", "") == "results"
    assert kind("Integrated Filing- Financial", "", "") is None
    assert kind("Outcome of Board Meeting", "", "X_OutcomewithFR.pdf") == "results"
    assert kind("Outcome of Board Meeting", "held for business matters", "x.pdf") == "meeting"
    announced = datetime(2024, 2, 10, 15, 0, tzinfo=IST)
    meeting = filings.Announcement(announced, announced, results=False)
    later = filings.Announcement(announced + timedelta(days=15), None, results=False)
    between = _filing(announced + timedelta(days=8), "xbrl")
    assert filings.judge(between, [meeting, later])[0] == "before_any_announcement"
    named = filings.Announcement(announced, announced)
    assert filings.judge(between, [named, later])[0] == "between_announcements"
    estimate = _filing(announced + timedelta(days=8), "deadline")
    assert filings.judge(estimate, [meeting])[0] == "estimate_unproven"
    assert filings.judge(estimate, [named])[0] == "late_vs_announcement"


def test_d5_parses_nse_times_and_matches_inside_the_period_window() -> None:
    assert filings.parse_time("30-Oct-2021 23:06:07") == datetime(
        2021, 10, 30, 23, 6, 7, tzinfo=IST
    )
    assert filings.parse_time("-") is None
    early = filings.Announcement(datetime(2023, 12, 20, tzinfo=IST), None)
    inside = filings.Announcement(datetime(2024, 2, 1, tzinfo=IST), None)
    late = filings.Announcement(datetime(2024, 6, 1, tzinfo=IST), None)
    assert filings.candidates([early, inside, late], date(2023, 12, 31)) == [inside]
    assert filings.lead_bucket(200) == "1-5 min"
    assert filings.lead_bucket(4000) == "over 1 h"


# --- D6 -----------------------------------------------------------------------------------------


def test_d6_the_calendar_against_the_files_and_the_store() -> None:
    summary, findings = sessions.check_sessions({D1, D2, D3}, set(), {D1, D2}, {D1}, {}, D1, D3)
    assert summary["calendar_sessions"] == 3
    codes = {f["code"]: f["items"] for f in findings}
    assert codes["session_without_bhavcopy"] == [D3.isoformat()]
    assert codes["session_not_stored"] == [D2.isoformat()]


def test_d6_a_weekday_with_no_calendar_row_fails_and_is_asked_of_nse() -> None:
    # second review item 8: D1 Fri, D2 Mon, D3 Tue; the calendar has no row for D3
    assert sessions.weekdays_between(D1, D3) == [D1, D2, D3]
    summary, findings = sessions.check_sessions(
        {D1, D2}, set(), {D1, D2}, {D1, D2}, {D3: references.FOUND}, D1, D3
    )
    codes = {f["code"]: f["items"] for f in findings}
    assert codes["weekday_without_calendar_row"] == [D3.isoformat()]
    assert codes["closed_weekday_has_bhavcopy"] == [D3.isoformat()]
    assert summary["closed_weekdays"] == 1


def test_d6_a_closed_weekday_is_asked_of_nse_not_of_the_calendars_own_list() -> None:
    # item 6: a trading day the calendar closed has no file under raw/ (it was never fetched);
    # NSE's archive answering for it fails, and an unasked closed weekday is unproven
    closed = {D2, D3}
    probes = {D2: references.FOUND}
    _, findings = sessions.check_sessions({D1}, closed, {D1}, {D1}, probes, D1, D3)
    codes = {f["code"]: f["items"] for f in findings}
    assert codes["closed_weekday_has_bhavcopy"] == [D2.isoformat()]
    assert codes["closed_weekday_unproven"] == [D3.isoformat()]
    _, ok = sessions.check_sessions(
        {D1}, closed, {D1}, {D1}, dict.fromkeys(closed, references.NOT_FOUND), D1, D3
    )
    assert ok == []


def test_d6_a_link_off_nses_symbol_changes_fails_dated() -> None:
    link = sessions.Link("o", "n", D2, "isin_change", "OLD", "NEW")
    spans = {"o": (D1, D1), "n": (D2, D3)}
    _, findings = sessions.check_links([link], spans, [("OTHER", "NEW")], [])
    unlisted = next(f for f in findings if f["code"] == "symbol_change_not_in_nse_list")
    assert (unlisted["severity"], unlisted["days"]) == (audit.FAIL, [D2.isoformat()])
    _, ok = sessions.check_links([link], spans, [("OLD", "NEW")], [])
    assert ok == []


def test_d6_no_symbol_change_list_fails() -> None:
    _, findings = sessions.check_links([], {}, None, [])
    assert [(f["code"], f["severity"]) for f in findings] == [
        ("symbol_changes_missing", audit.FAIL)
    ]


def test_d6_renames_chain_and_long_holes_are_bucketed() -> None:
    assert sessions.renamed([("A", "B"), ("B", "C")], "A", "C")
    assert not sessions.renamed([("A", "B")], "B", "A")
    days = [D1 + timedelta(days=n) for n in range(100)]
    gaps = sessions.suspension_gaps([("i", [days[0], days[30], days[31]])], days)
    assert gaps["gaps_by_sessions"] == {"21-62": 1}


# --- D7 -----------------------------------------------------------------------------------------


def test_d7_index_levels_match_exactly_and_blank_file_values_are_skipped() -> None:
    stored = {"NIFTY 50": [Decimal(1), Decimal(2), Decimal("0.5"), Decimal("1.5")]}
    source = {
        "NIFTY 50": (None, Decimal(2), Decimal("0.5"), Decimal("1.50")),
        "NIFTY 500": (Decimal(1), Decimal(1), Decimal(1), Decimal(1)),
    }
    wrong, missing = series.compare_index_day(date(2024, 3, 1), stored, source)
    assert wrong == []
    assert missing == [{"day": "2024-03-01", "index": "NIFTY 500"}]
    source["NIFTY 50"] = (None, Decimal(2), Decimal("0.5"), Decimal("1.55"))
    wrong, _ = series.compare_index_day(date(2024, 3, 1), stored, source)
    assert [w["field"] for w in wrong] == ["close"]


def test_d7_etf_factors_come_from_nses_unit_actions() -> None:
    # item 10: the factor is NSE's, not the store's own
    text = (
        "symbol,old_face_value_rs,new_face_value_rs,ex_effective_date\n"
        "NIFTYBEES,10,1,19-Dec-2019\n"
        "BAD,10,,19-Dec-2019\n"
    )
    actions, unread = series.read_unit_actions(text)
    assert unread == 1
    split = date(2019, 12, 19)
    assert series.unit_factor(actions["NIFTYBEES"], split - timedelta(days=1)) == Decimal("0.1")
    assert series.unit_factor(actions["NIFTYBEES"], split) == 1
    row = {
        "symbol": "NIFTYBEES",
        "date": D1,
        "open": 20_000_000,
        "high": 20_000_000,
        "low": 20_000_000,
        "close": 20_000_000,
        "factor": "1.0",
    }
    raw = {("NIFTYBEES", D1): [Decimal(2000)] * 4}
    assert series.compare_etf([row], raw, actions) == (1, [], [], [])
    old = date(2019, 1, 1)
    wrong = row | {"date": old, "factor": "1.0", "close": 2_000_000}
    raw[("NIFTYBEES", old)] = [Decimal(2000)] * 4
    compared, prices, missing, factors = series.compare_etf([wrong], raw, actions)
    assert [f["nse"] for f in factors] == ["0.1"]
    assert {p["field"] for p in prices} == {"open", "high", "low"}  # / 0.1, not / 1.0
    assert (compared, missing) == (1, [])


def test_d7_reads_the_tri_answers_on_its_own_and_fails_their_gaps() -> None:
    # item 8: an own reader, and a span no answer covers fails (11: whole absent days seen)
    rows = [
        {"Date": "01 Mar 2024", "TotalReturnsIndex": "1,000.5"},
        {"Date": "bad", "TotalReturnsIndex": "1"},
    ]
    answer = series.read_tri_answer(
        "NIFTY_500_TRI_20240301_20240304.json", json.dumps(rows).encode()
    )
    assert answer is not None
    assert (answer.first, answer.last, answer.unread) == (D1, D2, 1)
    assert answer.closes == {D1: Decimal("1000.5")}
    assert series.read_tri_answer("other.json", b"[]") is None
    out = series.compare_tri(
        {D1: Decimal("1000.5"), D2: Decimal(9)}, [answer], [D1, D2, D3], D1, D3
    )
    assert out["tri_not_in_niftyindices"] == [D2.isoformat()]
    assert out["tri_unanswered"] == [D3.isoformat()]
    assert "tri_mismatch" not in out


def test_d7_a_stored_day_in_an_unfetched_earlier_year_is_unanswered_not_missing() -> None:
    # CodeAnt: only a later year's answer is kept; an earlier stored day is outside every
    # answer's span, so it is unanswered (no reference), never "not in niftyindices"
    later = series.TriAnswer(D2, D3, {D2: Decimal(10), D3: Decimal(11)}, 0)
    early = date(2023, 6, 1)
    out = series.compare_tri(
        {early: Decimal(5), D2: Decimal(10), D3: Decimal(11)}, [later], [early, D2, D3], early, D3
    )
    assert "tri_not_in_niftyindices" not in out
    assert out["tri_unanswered"] == [early.isoformat()]


def test_d5_announcements_with_only_symbols_still_give_the_reference_end() -> None:
    # CodeAnt: rows with a symbol and no ISIN must not abort D5
    at = datetime(2024, 3, 2, 18, tzinfo=IST)
    said = filings.Announcement(at, None)
    assert filings.last_announcement({}, {"ABC": [said]}) == at
    assert filings.last_announcement({}, {}) is None


def test_d3_a_capture_is_judged_on_the_session_it_shows() -> None:
    # CodeAnt: a closed day's list shows the session before, judged on that session (D1 Fri,
    # D2 Mon): a Saturday capture is Friday's, never Monday's
    sessions = [D1, D2, D3]
    assert membership.list_session(sessions, D1 + timedelta(days=1)) == D1
    assert membership.list_session(sessions, D2) == D2
    assert membership.list_session(sessions, D1 - timedelta(days=1)) is None


def test_d2_a_reused_ticker_never_pulls_in_another_chain() -> None:
    # CodeAnt: ABC is now another company's ticker; the reference names the old ISIN
    by_key = {"ABC": {"old", "new_company"}, "INE111A01011": {"old"}}
    assert adjustments.ref_instruments({"ABC", "INE111A01011"}, by_key) == ["old"]
    assert adjustments.ref_instruments({"ABC", "INE999Z01019"}, by_key) == ["new_company", "old"]
    ref = RefAction("split_bonus", D2, frozenset({"ABC", "INE111A01011"}), Fraction(1, 2))
    keys = {"old": frozenset({"ABC", "INE111A01011"}), "new_company": frozenset({"ABC"})}
    stored = [_stored_action("new_company", "split", "0.5")]  # only the unrelated chain's action
    summary, _ = adjustments.compare_factors([ref], stored, keys, {}, [], ALL_KINDS)
    assert summary.get("split_bonus:equal") is None
    assert summary["split_bonus:not_stored"] == 1


def test_an_older_pass_never_overrides_a_newer_failure_in_the_window() -> None:
    # second Opus review item 2: the newer, wider report failed D2 inside the window and went
    # stale from a change outside it; the older, narrower pass on the data now must not verify
    bad = verify.dated(audit.finding("x", audit.FAIL, []), [D2])
    newer = verify.report_doc(
        {"D2": _check("fail", bad)},
        verify.FIRST_DAY,
        D3,
        datetime(2026, 11, 1, tzinfo=UTC),
        {"digest": "wide-old"},
    )
    older = verify.report_doc(
        {"D2": _check("pass")}, D1, D3, datetime(2026, 10, 1, tzinfo=UTC), SNAP
    )
    reports = [(newer, None), (older, None)]
    assert verify.pick_reports(reports, "D2", lambda d: "data-1", D1, D3) == "stale"
    status = verify.window_status(reports, D1, D3, lambda d: "data-1", used_5m=False)
    assert status["verdicts"]["D2"] == "stale"
    assert not status["verified"]
    # a failure outside the window does not stop the older pass from judging it
    assert verify.pick_reports(reports, "D2", lambda d: "data-1", D3, D3) == (older, None)


def _say(*days: date, results: bool = True) -> list[filings.Announcement]:
    return [
        filings.Announcement(datetime.combine(d, datetime.min.time(), IST), None, results)
        for d in days
    ]


UNLISTED = filings.Listing(None, None)
COVERAGE = date(2020, 3, 31)  # the store's first quarter (`FIRST_PERIOD_END`)


def _quarters_missing(  # noqa: PLR0913
    stored: list[date],
    said: list[filings.Announcement],
    as_of: date,
    *,
    listing: filings.Listing = UNLISTED,
    coverage: date = COVERAGE,
    start: date = date(2016, 1, 1),
) -> list[tuple[str, str]]:
    holes = filings.missing_quarters(
        [("INE1",)],
        {"INE1": stored},
        {"INE1": listing},
        {"INE1": said},
        coverage,
        as_of,
        start,
        as_of,
    )
    return [(i["quarter"], i["day"]) for i in holes]


def test_d5_announcements_never_excuse_a_missing_quarter() -> None:
    # Dec-25 and Mar-26 stored, Jun-26 announced 2026-07-15, not stored
    stored = [date(2025, 12, 31), date(2026, 3, 31)]
    listed = filings.Listing(date(2025, 11, 1), None)
    assert _quarters_missing(
        stored, _say(date(2026, 7, 15)), date(2026, 10, 29), listing=listed
    ) == [("2026-06-30", "2026-07-15")]
    # ... and with no announcement at all, dated by its deadline
    assert _quarters_missing(stored, [], date(2026, 10, 29), listing=listed) == [
        ("2026-06-30", "2026-08-14")
    ]


def test_d5_demand_starts_at_the_store_or_the_listing_never_at_stored_data() -> None:
    # a company listed long ago with nothing stored owes every quarter from the store's first
    as_of = date(2020, 12, 31)
    assert _quarters_missing([], [], as_of, listing=filings.Listing(date(1995, 1, 1), None)) == [
        ("2020-03-31", "2020-05-30"),
        ("2020-06-30", "2020-08-14"),
        ("2020-09-30", "2020-11-14"),
    ]
    # its first stored period does not move the start: Mar-20 and Jun-20 are still owed
    stored = [date(2020, 9, 30)]
    assert _quarters_missing(stored, [], as_of, listing=filings.Listing(None, None)) == [
        ("2020-03-31", "2020-05-30"),
        ("2020-06-30", "2020-08-14"),
    ]
    # a company listed in Feb-21 owes Mar-21 onwards
    listed = filings.Listing(date(2021, 2, 10), None)
    assert _quarters_missing([date(2021, 6, 30)], [], date(2021, 9, 30), listing=listed) == [
        ("2021-03-31", "2021-05-30")
    ]


def test_d5_every_quarter_stored_passes_and_the_tail_waits_for_its_deadline() -> None:
    stored = [date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31)]
    said = _say(date(2023, 4, 20), date(2023, 7, 20), date(2023, 10, 18), date(2024, 1, 19))
    listed = filings.Listing(date(2023, 1, 2), None)
    assert _quarters_missing(stored, said, date(2024, 2, 14), listing=listed) == []
    # Mar-24 is due 2024-05-30: not demanded the day before, demanded on it
    assert filings.deadline(date(2024, 3, 31)) == date(2024, 5, 30)
    assert _quarters_missing(stored, said, date(2024, 5, 29), listing=listed) == []
    assert _quarters_missing(stored, said, date(2024, 5, 30), listing=listed) == [
        ("2024-03-31", "2024-05-30")
    ]


def test_d5_a_late_covid_filer_is_dated_by_its_announcement_within_grace() -> None:
    # Mar-20 announced 2020-07-20, 51 days past its deadline: beyond GRACE, dated by the deadline
    stored = [date(2020, 6, 30)]
    assert _quarters_missing(stored, _say(date(2020, 7, 20)), date(2020, 9, 30)) == [
        ("2020-03-31", "2020-05-30")
    ]
    # announced 2020-06-25 (within GRACE of the deadline): dated by the announcement
    assert _quarters_missing(stored, _say(date(2020, 6, 25)), date(2020, 9, 30)) == [
        ("2020-03-31", "2020-06-25")
    ]
    # stored late, every quarter there: nothing missing
    stored = [date(2020, 3, 31), date(2020, 6, 30)]
    assert _quarters_missing(stored, _say(date(2020, 7, 20)), date(2020, 9, 30)) == []


def test_d5_a_quarter_is_owed_only_if_due_by_the_last_traded_day() -> None:
    stored = [date(2021, 6, 30), date(2021, 9, 30)]
    listed = filings.Listing(date(2021, 4, 10), date(2021, 12, 1))
    # delisted 2021-12-01: Sep-21 (due 2021-11-14) is owed, Dec-21 (due 2022-02-14) is not
    assert _quarters_missing(stored, [], date(2022, 12, 31), listing=listed) == []
    gone = filings.Listing(date(2021, 4, 10), date(2021, 11, 13))
    assert _quarters_missing([date(2021, 6, 30)], [], date(2022, 12, 31), listing=gone) == []
    assert _quarters_missing([date(2021, 6, 30)], [], date(2022, 12, 31), listing=listed) == [
        ("2021-09-30", "2021-11-14")
    ]
    # a finding outside the window is dropped
    holes = filings.missing_quarters(
        [("INE1",)],
        {"INE1": [date(2021, 6, 30)]},
        {"INE1": listed},
        {},
        COVERAGE,
        date(2022, 12, 31),
        date(2021, 11, 15),
        date(2022, 12, 31),
    )
    assert holes == []
    # announced before the window: dated by that announcement, not by its deadline inside it
    said = {"INE1": [filings.Announcement(_at(2021, 11, 10, 16, 0), None)]}
    holes = filings.missing_quarters(
        [("INE1",)],
        {"INE1": [date(2021, 6, 30)]},
        {"INE1": listed},
        said,
        COVERAGE,
        date(2022, 12, 31),
        date(2021, 11, 12),
        date(2022, 12, 31),
    )
    assert holes == []


def test_d5_a_company_is_judged_over_its_isin_chain() -> None:
    # INE1 became INE2 on 2022-08-01 (linked): Jun-22 filed under INE1, Sep-22 under INE2
    chains = filings.lineage_chains([("INE1", "INE2")], {"INE2", "INE9"})
    assert chains == [("INE1", "INE2"), ("INE9",)]
    listings = {
        "INE1": filings.Listing(date(2010, 1, 4), date(2022, 7, 29)),
        "INE2": filings.Listing(date(2022, 8, 1), date(2026, 10, 28)),
    }
    stored = {"INE1": [date(2022, 6, 30)], "INE2": [date(2022, 9, 30)]}
    holes = filings.missing_quarters(
        [("INE1", "INE2")],
        stored,
        listings,
        {},
        date(2022, 6, 30),
        date(2022, 12, 31),
        date(2016, 1, 1),
        date(2022, 12, 31),
    )
    assert holes == []  # neither ISIN owes the other's quarter
    # a quarter neither stored is owed once, named by the latest ISIN
    holes = filings.missing_quarters(
        [("INE1", "INE2")],
        {"INE1": [date(2022, 6, 30)]},
        listings,
        {},
        date(2022, 6, 30),
        date(2022, 12, 31),
        date(2016, 1, 1),
        date(2022, 12, 31),
    )
    assert [(i["isin"], i["isins"], i["quarter"]) for i in holes] == [
        ("INE2", ["INE1", "INE2"], "2022-09-30")
    ]
    assert filings.chain_listing(("INE1", "INE2"), listings) == filings.Listing(
        date(2010, 1, 4), date(2026, 10, 28)
    )


def test_d5_reviewed_exceptions_cover_one_quarter_of_one_company(tmp_path: Path) -> None:
    rules = [
        filings.Excepted("results_quarter_missing", "INE1", "2022-06-30", "NSE lists no XBRL"),
        filings.Excepted("unmatched", "INE7", "2024-03-31", "files with BSE only"),
    ]
    holes = [
        {"isin": "INE2", "isins": ["INE1", "INE2"], "quarter": "2022-06-30"},
        {"isin": "INE2", "isins": ["INE1", "INE2"], "quarter": "2022-09-30"},
    ]
    failing, excepted, used = filings.except_items("results_quarter_missing", holes, rules)
    assert failing == [holes[1]]
    assert [(e["quarter"], e["reason"]) for e in excepted] == [("2022-06-30", "NSE lists no XBRL")]
    assert used == {rules[0]}
    # another finding: its exact ISIN and period only (a chain's other ISIN, a new quarter fail)
    timing = [
        {"isin": "INE7", "isins": ["INE6", "INE7"], "period_end": "2024-03-31"},
        {"isin": "INE7", "period_end": "2024-06-30"},
        {"isin": "INE6", "isins": ["INE6", "INE7"], "period_end": "2024-03-31"},
    ]
    failing, excepted, used = filings.except_items("unmatched", timing, rules)
    assert failing == timing[1:]
    assert used == {rules[1]}
    # an exception for another finding code covers nothing
    assert filings.except_items("estimate_unproven", timing, rules)[0] == timing
    # every exception names a period end and a reason: no wildcard
    path = tmp_path / "x.csv"
    path.write_text("code,isin,symbol,period_end,reason\nunmatched,INE7,X,*,BSE only\n")
    with pytest.raises(ValueError, match="period end"):
        filings.read_exceptions(path)


def _at(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=IST)


def test_d5_an_xbrl_time_at_its_listing_broadcast_is_in_order() -> None:
    filing = filings.Filing(
        "X", "INE1", date(2026, 3, 31), "nse", _at(2026, 5, 22, 15, 22, 22), "xbrl", "u.xml"
    )
    later = [filings.Announcement(_at(2026, 5, 25, 18, 38, 28), _at(2026, 5, 25, 18, 38, 30))]
    assert filings.judge(filing, later)[0] == "before_any_announcement"
    assert filings.judge(filing, later, _at(2026, 5, 22, 15, 22, 8))[0] is None
    # a broadcast after public_at proves nothing
    assert filings.judge(filing, later, _at(2026, 5, 22, 16, 0))[0] == "before_any_announcement"

    assert filings.quarter_of(date(2024, 2, 29)) == date(2024, 3, 31)
    assert filings.next_quarter(date(2024, 12, 31)) == date(2025, 3, 31)


def test_a_newer_stale_failure_counts_even_outside_its_coverage() -> None:
    # third review item 5: a narrower newer report (coverage ending early) with a failure in the
    # window still stops an older pass
    bad = verify.dated(audit.finding("x", audit.FAIL, []), [D2])
    newer = verify.covered(_check("fail", bad), D1, D2)
    doc = verify.report_doc(
        {"D5": newer}, verify.FIRST_DAY, D3, datetime(2026, 11, 1, tzinfo=UTC), {"digest": "old"}
    )
    older = verify.report_doc(
        {"D5": _check("pass")}, D1, D3, datetime(2026, 10, 1, tzinfo=UTC), SNAP
    )
    assert verify.check_status(newer, D1, D3) == "not_run"
    assert (
        verify.pick_reports([(doc, None), (older, None)], "D5", lambda d: "data-1", D1, D3)
        == "stale"
    )


def test_d3_the_live_list_is_fetched_for_a_run_to_the_last_session_by_today() -> None:
    # third review item 6: the calendar lists the year's coming sessions
    sessions = [D1, D2, D3, date(2024, 12, 31)]
    assert membership.last_session_by(sessions, D3) == D3
    assert membership.last_session_by(sessions, date(2024, 3, 3)) == D1
    last = membership.last_session_by(sessions, D3)
    assert membership.live_list_name(D3, D3, last) is not None


def test_d5_a_pre_listing_quarter_is_matched_near_its_public_at() -> None:
    # Sep-23 of a company listed in 2024 counts from Sep-24's results (its comparative)
    said = [filings.Announcement(_at(2024, 11, 8, 16, 0), _at(2024, 11, 8, 16, 1))]
    filing = filings.Filing(
        "X", "INE1", date(2023, 9, 30), "secondary", _at(2024, 11, 8, 16, 1), "announcement"
    )
    assert filings.candidates(said, filing.period_end) == []
    found = filings.near(said, filing.period_end, filing.public_at)
    assert found == said
    assert filings.judge(filing, found)[0] is None
    # matched on its dissemination, a day after the submission
    late = [filings.Announcement(_at(2024, 11, 7, 10, 0), _at(2024, 11, 8, 16, 1))]
    assert filings.near(late, filing.period_end, filing.public_at + timedelta(hours=20)) == late


@pytest.mark.parametrize(
    ("text", "cash"),
    [
        ("Interim Dividend - Rs 9 Per Share9purpose Revised", "9"),  # `Share9` is not `Re 9`
        ("Dividend - Rs:- 5.50 Per Share", "5.50"),
        ("Dividend 0.70/- Per Share", "0.70"),
        ("Dividend 12.50/-+Special Dividend 0.50/-", "13.00"),
        ("Dividend - 1 Rs Per Share", "1"),
        ("Div-7.5/-", "7.5"),
        ("Dividend - Re 0.50 Per Equity Share Re.1/- Each", "0.50"),  # the face value is not paid
        ("Annual General Meetingdividend - Rs 2 Per Share", "2"),
        ("Intdividend Rs 3", "3"),
        ("Interim Dividend", None),  # no amount: judged by presence
    ],
)
def test_d2_dividend_texts_nse_writes(text: str, cash: str | None) -> None:
    assert adjustments.names_dividend(text)
    assert adjustments.dividend_cash(text) == (None if cash is None else Decimal(cash))


def test_d2_rights_at_par_and_prm() -> None:
    assert adjustments.issue_price("Rights 2:3@ Par Rs 10", "", Decimal(10)) == 10
    assert adjustments.issue_price("Rights 24:10 At Par", "", Decimal(1)) == 1
    assert adjustments.issue_price("Rights 1:5 @ Prm Rs 102", "", Decimal(10)) == 112
    assert adjustments.issue_price("Rights 1:5", "", Decimal(10)) is None


def test_d2_a_bonus_instruments_value_is_not_the_dividend() -> None:
    # COROMANDEL 2012: stored 18 = bonus debentures 15 + final 3; NSE's purpose prices the 3
    text = "Dividend bonus_instrument 15 + final 3: Annual General Meeting/Dividend Rs.3/-"
    stored = StoredAction("a", D2, "dividend", Fraction(1), Decimal(18), False, "feed", text)
    assert stored.payout == 3
    ref = RefAction("dividend", D2, frozenset({"ABC"}), cash=Decimal(3), amounts=(Decimal(3),))
    summary, _ = adjustments.compare_factors(
        [ref], [stored], {"a": frozenset({"ABC"})}, {}, [], ALL_KINDS
    )
    assert summary == {"dividend:equal": 1}


def test_d2_a_dividend_that_is_only_a_bonus_instrument_is_proven_by_nses_bonus_row() -> None:
    # BRITANNIA 2019: bonus debentures worth Rs 30, no dividend in NSE's purpose
    text = "Dividend bonus_instrument 30: Bonus Debentures 1:1"
    stored = StoredAction("a", D2, "dividend", Fraction(1), Decimal(30), False, "research", text)
    ref = RefAction("split_bonus", D2, frozenset({"ABC"}), Fraction(1, 2), non_equity=True)
    keys = {"a": frozenset({"ABC"})}
    summary, findings = adjustments.compare_factors([ref], [stored], keys, {}, [], ALL_KINDS)
    assert summary == {"split_bonus:non_equity_not_stored": 1, "dividend:bonus_instrument": 1}
    assert findings == []
    _, alone = adjustments.compare_factors([], [stored], keys, {}, [], ALL_KINDS)
    assert [f["code"] for f in alone] == ["dividend_stored_not_in_reference"]


def test_d2_a_non_equity_bonus_without_a_ratio_proves_its_day_only() -> None:
    # BLUEDART 2014-11-17: "Scheme Of Arrangement - Issue Of Bonus Debentures", no ratio
    row = {"symbol": "BLUEDART", "isin": "INE233B01017", "action_type": "bonus"}
    row |= {"subject_raw": "Scheme Of Arrangement - Issue Of Bonus Debentures"}
    row |= {"instrument_class": "debentures", "exDate": "17-Nov-2014", "ratio_parsed": ""}
    ref = adjustments._split_bonus(row)
    assert ref is not None
    assert ref.factor is None
    assert ref.non_equity
    equity = {**row, "instrument_class": "equity"}
    assert adjustments._split_bonus(equity) is None  # an equity bonus with no ratio is unread
    split = RefAction("split_bonus", D2, frozenset({"ABC"}), Fraction(1, 2), text="split")
    assert adjustments.combine_legs([ref, split]).factor == Fraction(1, 2)
    text = "Dividend bonus_instrument 140: Bonus NCDs"
    stored = StoredAction("a", D2, "dividend", Fraction(1), Decimal(140), False, "research", text)
    unpriced = RefAction("split_bonus", D2, frozenset({"ABC"}), None, non_equity=True)
    summary, findings = adjustments.compare_factors(
        [unpriced], [stored], {"a": frozenset({"ABC"})}, {}, [], ALL_KINDS
    )
    assert summary == {"split_bonus:non_equity_unpriced": 1, "dividend:bonus_instrument": 1}
    assert findings == []


def test_d2_funds_are_not_judged() -> None:
    keys = {"etf": frozenset({"GOLDBEES", "INF732E01102"}), "s": frozenset({"ABC", "INE1A01011"})}
    ref = RefAction("split_bonus", D2, frozenset({"GOLDBEES", "INF732E01102"}), Fraction(1, 10))
    stored = [_stored_action("etf", "split", "0.1"), _stored_action("s", "split", "0.5")]
    summary, findings = adjustments.compare_factors([ref], stored, keys, {}, [], ALL_KINDS)
    assert summary == {
        "split_bonus:fund_not_judged": 2,
        "split_bonus:stored_not_in_reference": 1,
    }
    assert [f["items"][0]["keys"] for f in findings] == [["ABC", "INE1A01011"]]


def test_d2_absent_rows_that_prove_nothing_do_not_fail() -> None:
    keys = {"a": frozenset({"ABC"})}
    refs = [
        RefAction("split_bonus", D2, frozenset({"ABC"}), Fraction(1, 2), non_equity=True),
        RefAction("dividend", D2, frozenset({"ABC"})),  # `Interim Dividend`: no amount
        RefAction("dividend", D3, frozenset({"ABC"}), cash=Decimal(1), amounts=(Decimal(1),)),
    ]
    summary, findings = adjustments.compare_factors(refs, [], keys, {}, [], ALL_KINDS)
    assert summary == {
        "split_bonus:non_equity_not_stored": 1,
        "dividend:not_stored_unpriced": 1,
        "dividend:no_bar": 1,  # no close before D3: nothing to adjust
    }
    assert {f["severity"] for f in findings} == {audit.WARN}


def test_d2_a_stale_isin_falls_back_to_the_tickers_holder_on_the_day() -> None:
    # KSE 2026: NSE's row names the old ISIN; the dividend is stored on the relisted one
    keys = {"old": frozenset({"KSE", "INE953E01014"}), "new": frozenset({"KSE", "INE953E01022"})}
    held = {"KSE": [(D1, D2, frozenset({"old"})), (D2, None, frozenset({"new"}))]}
    ref = RefAction(
        "dividend", D3, frozenset({"KSE", "INE953E01014"}), cash=Decimal(7), amounts=(Decimal(7),)
    )
    stored = [StoredAction("new", D3, "dividend", Fraction(1), Decimal(7), False, "feed")]
    summary, _ = adjustments.compare_factors([ref], stored, keys, {}, [], ALL_KINDS, held)
    assert summary == {"dividend:equal": 1}


def test_d2_stored_dividends_after_the_pull_and_excepted_items() -> None:
    keys = {"a": frozenset({"ABC", "INE1A01011"})}
    late = StoredAction("a", D3, "dividend", Fraction(1), Decimal(2), False, "nse_pr_bc")
    rule = Excepted("split_bonus_stored_not_in_reference", "INE1A01011", D2.isoformat(), "known")
    exceptions = Exceptions([rule])
    summary, findings = adjustments.compare_factors(
        [],
        [late, _stored_action("a", "split", "0.5")],
        keys,
        {},
        [],
        ALL_KINDS,
        ends={"dividend": D3},
        exceptions=exceptions,
    )
    assert summary == {"split_bonus:stored_not_in_reference": 1}
    assert findings == []
    assert exceptions.summary() == {"split_bonus_stored_not_in_reference": 1}
    assert [f["code"] for f in exceptions.findings()] == ["excepted"]


def test_d2_a_drop_the_size_of_a_stored_dividend_is_explained() -> None:
    # FOURSOFT 2013-10-17: Rs 29 paid on a close of 42.15, the price drops 67%
    move = {"ex_date": False, "move_pct": -67.26, "raw_move_pct": -67.26}
    assert adjustments.classify_move(move, False, 42.15, 29.0) == "dividend"
    assert adjustments.classify_move(move, False, 42.15, 5.0) != "dividend"


def test_d2_chains_of_joins_runs_of_links() -> None:
    chains = adjustments.chains_of(["a", "b", "c", "d"], [("a", "b"), ("b", "c")])
    assert chains["a"] == chains["c"] == frozenset({"a", "b", "c"})
    assert chains["d"] == frozenset({"d"})


def test_d6_a_link_is_proven_by_nse_rows_across_its_chain() -> None:
    link = sessions.Link(
        "o",
        "n",
        D2,
        "isin_change",
        "OLD",
        "NEW",
        "INE1A01011",
        "INE1A01029",
        frozenset({"OLD", "NEW", "INE1A01011", "INE1A01029"}),
    )
    far = sessions.Evidence(D2 + timedelta(days=sessions.LINK_DAYS + 1), "INE1A01011", "NEW")
    assert sessions.link_evidence(link, [far]) == (False, False)
    # NSE files the old ISIN's split under today's ticker: proves the link and the rename
    assert sessions.link_evidence(link, [sessions.Evidence(D2, "INE1A01011", "NEW")]) == (
        True,
        True,
    )
    assert sessions.link_evidence(link, [sessions.Evidence(D2, "INE1A01011", "OLD")]) == (
        True,
        False,
    )


def test_d6_listing_judges_stocks_and_counts_the_rest() -> None:
    listings = [
        sessions.Listing("s", "ABC", False, None, "INE002A01018"),
        sessions.Listing("f", "GOLDBEES", False, None, "INF732E01102"),
        sessions.Listing("r", "ABC-RE", False, None, "INE002A20018"),  # a rights entitlement
    ]
    last = {"s": D3, "f": D3, "r": D3}
    summary, findings = sessions.check_listing(listings, last, [D1, D2, D3], ())
    assert [(f["code"], f["severity"]) for f in findings] == [("inactive_but_trading", audit.FAIL)]
    assert summary["not_stocks_not_judged"] == {"inactive_but_trading": 2}


def test_exceptions_match_one_item_by_isin_and_day() -> None:
    rule = Excepted("move_ex_date_jump", "INE098201036", "2023-02-15", "penny")
    exceptions = Exceptions([rule, Excepted("x", "INE1", "2020-01-01", "never used")])
    move = {"date": "2023-02-15", "ex_date": True, "isin": "INE098201036", "move_pct": 21.0}
    other = {**move, "date": "2023-02-16"}
    assert exceptions.split("move_ex_date_jump", [move, other]) == [other]
    assert [f["code"] for f in exceptions.findings()] == ["excepted", "exception_unused"]
