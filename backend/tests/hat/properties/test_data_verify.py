"""The data verification's pure seams (`app.hat.data.verify`): the comparators, parsers and
classifiers of each check on hand-built rows, and the report's window status the data audit
reads."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

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
from app.hat.time import IST

D1, D2, D3 = date(2024, 3, 1), date(2024, 3, 4), date(2024, 3, 5)


# --- the report and the data audit --------------------------------------------------------------


SNAP = {"digest": "data-1", "parts": {}}


def _check(status: str, *findings: dict, coverage: tuple[date, date] | None = None) -> dict:
    result = {"status": status, "summary": {}, "findings": list(findings)}
    return result if coverage is None else verify.covered(result, *coverage)


def _report(statuses: dict[str, str], start: date = D1, end: date = D3) -> dict:
    checks = {
        k: _check(s, *([audit.finding("x", audit.FAIL, [])] if s == "fail" else []))
        for k, s in statuses.items()
    }
    return verify.report_doc(checks, start, end, datetime(2026, 10, 29, tzinfo=UTC), SNAP)


def _status(doc: dict | None, start: date = D1, end: date = D3, **kw: object) -> dict:
    args = {"current": "data-1", "used_5m": False} | kw
    return verify.window_status(doc, None, start, end, **args)  # type: ignore[arg-type]


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
    assert (stale["verified"], stale["stale"]) == (False, True)
    found = verify.window_status(doc, Path("v.json"), D1, D3, "data-2", used_5m=False)
    assert audit.check_verify(found)["findings"][0]["code"] == "verify_report_stale"


def test_the_snapshot_reads_each_store_files_day() -> None:
    assert snapshot.file_day("bars_5m/date=2024-03-01/bars.parquet") == D1
    assert snapshot.file_day("raw/cm01MAR2024bhav.csv.zip") == D1
    assert snapshot.file_day("raw/ind_close_all_01032024.csv") == D1
    assert snapshot.file_day("raw/BhavCopy_NSE_CM_0_0_0_20240301_F_0000.csv.zip") == D1
    assert snapshot.file_day("raw/reference/nse/symbolchange.csv") is None
    a = snapshot.files_digest([("p", 1, 2)])
    assert a == snapshot.files_digest([("p", 1, 2)])
    assert a != snapshot.files_digest([("p", 1, 3)])  # rewritten in place


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
        verify.write_report(verify.reports_dir(tmp_path) / f"verify-{name}", doc, "")
    found = verify.covering_reports(tmp_path, D1, D3)
    assert [d["digest"] for d, _ in found] == [new["digest"], old["digest"]]
    assert verify.covering_reports(tmp_path, D1, D3 + timedelta(days=1)) == []


def test_a_narrow_run_never_replaces_the_full_report(tmp_path: Path) -> None:
    full = verify.report_folder(tmp_path, D1, verify.FIRST_DAY, D3, sorted(verify.CHECKS))
    narrow = verify.report_folder(tmp_path, D1, D2, D3, ["D2"])
    assert full.name == "verify-2024-03-01"
    assert narrow.name == "verify-2024-03-01-D2-20240304-20240305"


def test_the_audit_warns_an_unverified_window_and_never_fails_it() -> None:
    missing = audit.check_verify(_status(None))
    assert missing["status"] == audit.WARN
    assert missing["findings"][0]["code"] == "verify_report_missing"
    failed = audit.check_verify(
        verify.window_status(
            _report({"D1": "fail"}), Path("v.json"), D1, D3, "data-1", used_5m=False
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
    summary, _ = adjustments.compare_factors([two], stored, keys, {}, [], D1)
    assert summary == {"dividend:cash_mismatch": 1}
    repeated = RefAction(
        "dividend", D2, frozenset({"ABC"}), cash=Decimal(10), amounts=(Decimal(5), Decimal(5))
    )
    summary, _ = adjustments.compare_factors([repeated], stored, keys, {}, [], D1)
    assert summary == {"dividend:equal_one_row": 1}


def test_d2_a_reference_action_of_no_instrument_is_a_counted_finding() -> None:
    ref = RefAction("split_bonus", D2, frozenset({"NOHAT"}), Fraction(1, 2))
    _, findings = adjustments.compare_factors([ref], [], {}, {}, [], D1)
    assert [(f["code"], f["severity"], f["count"]) for f in findings] == [
        ("split_bonus_no_instrument", audit.WARN, 1)
    ]


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
    summary, findings = adjustments.compare_factors([ref], stored, keys, {}, [], D1)
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
    summary, findings = adjustments.compare_factors(
        refs, stored, keys, {}, [("b", D2, "split")], D1
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
    _, findings = sessions.check_links([link], spans, [("OTHER", "NEW")])
    unlisted = next(f for f in findings if f["code"] == "symbol_change_not_in_nse_list")
    assert (unlisted["severity"], unlisted["days"]) == (audit.FAIL, [D2.isoformat()])
    _, ok = sessions.check_links([link], spans, [("OLD", "NEW")])
    assert ok == []


def test_d6_no_symbol_change_list_fails() -> None:
    _, findings = sessions.check_links([], {}, None)
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
