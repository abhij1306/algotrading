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
from app.hat.data.verify import adjustments, bars5m, filings, membership, rawbars, series, sessions
from app.hat.data.verify.adjustments import RefAction, StoredAction
from app.hat.time import IST

D1, D2, D3 = date(2024, 3, 1), date(2024, 3, 4), date(2024, 3, 5)


# --- the report and the data audit --------------------------------------------------------------


def _report(statuses: dict[str, str], start: date = D1, end: date = D3) -> dict:
    checks = {k: {"status": s, "summary": {}, "findings": []} for k, s in statuses.items()}
    return verify.report_doc(checks, start, end, datetime(2026, 10, 29, tzinfo=UTC))


def test_the_digest_ignores_the_creation_time() -> None:
    a = _report({"D1": "pass"})
    b = verify.report_doc(
        {"D1": {"status": "pass", "summary": {}, "findings": []}},
        D1,
        D3,
        datetime(2027, 1, 1, tzinfo=UTC),
    )
    assert a["digest"] == b["digest"]
    assert a["created"] != b["created"]


def test_a_failed_blocking_check_leaves_the_window_unverified() -> None:
    every = dict.fromkeys(verify.CHECKS, "pass")
    assert verify.window_status(_report(every), None, used_5m=True)["verified"]
    failed = verify.window_status(_report(every | {"D2": "fail"}), None, used_5m=False)
    assert (failed["verified"], failed["failed"]) == (False, ["D2"])


def test_d4_blocks_only_a_run_that_reads_5_minute_bars() -> None:
    doc = _report(dict.fromkeys(verify.CHECKS, "pass") | {"D4": "fail"})
    assert verify.window_status(doc, None, used_5m=False)["verified"]
    assert verify.window_status(doc, None, used_5m=True)["failed"] == ["D4"]


def test_a_blocking_check_not_run_leaves_the_window_unverified() -> None:
    status = verify.window_status(_report({"D1": "pass"}), None, used_5m=False)
    assert not status["verified"]
    assert "D2" in status["not_run"]


def test_the_newest_report_covering_the_window_is_read(tmp_path: Path) -> None:
    old = _report(dict.fromkeys(verify.CHECKS, "fail"))
    new = verify.report_doc(
        {k: {"status": "pass"} for k in verify.CHECKS},
        D1,
        D3,
        datetime(2026, 10, 30, tzinfo=UTC),
    )
    narrow = verify.report_doc({}, D2, D3, datetime(2026, 10, 31, tzinfo=UTC))
    for name, doc in (("a", old), ("b", new), ("c", narrow)):
        verify.write_report(verify.reports_dir(tmp_path) / f"verify-{name}", doc, "")
    found = verify.covering_reports(tmp_path, D1, D3)
    assert [d["digest"] for d, _ in found] == [new["digest"], old["digest"]]
    assert verify.status_for(tmp_path, D1, D3, used_5m=False)["verified"]
    assert verify.status_for(tmp_path, D1, D3 + timedelta(days=1), used_5m=False)["report"] is None


def test_the_audit_warns_an_unverified_window_and_never_fails_it() -> None:
    missing = audit.check_verify(verify.window_status(None, None, used_5m=False))
    assert missing["status"] == audit.WARN
    assert missing["findings"][0]["code"] == "verify_report_missing"
    failed = audit.check_verify(
        verify.window_status(_report({"D1": "fail"}), Path("v.json"), used_5m=False)
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


def test_d2_rights_terp_and_the_face_value_the_reference_does_not_date() -> None:
    assert adjustments.rights_factor(1, 1, Decimal(50), Decimal(100)) == Fraction(3, 4)
    assert adjustments.rights_factor(1, 1, Decimal(120), Decimal(100)) == 1
    prices = adjustments.issue_prices("Rights 1:3 @ Premium Rs 40/-", "", "1")
    assert prices[0] == 41
    assert Decimal(50) in prices
    stored = adjustments.rights_factor(1, 3, Decimal(50), Decimal(100))
    code, _ = adjustments.judge_rights(stored, 1, 3, prices, Decimal(100))
    assert code == "equal_other_face_value"
    assert adjustments.issue_prices("Rights 2:7", "", "10") == []
    assert adjustments.issue_prices("Rights 1:2 @ Rs 150 Per Share", "", "10") == [150]


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
    traced, entries, exits = membership.trace_changes(
        members,
        [*changes, membership.NoticeChange("n3", (D3,), frozenset(), frozenset({"FORMER"}))],
        set(),
        [D1, D2, D3],
        D1,
        {"i-ren": {"FORMER"}},
    )
    assert traced == 3
    assert [e["symbol"] for e in entries] == ["ODD"]
    assert exits == []


def test_d3_an_archived_list_difference_is_explained_or_named() -> None:
    listed = {"INE1": "A", "DUM545A01024": "DUMMYX", "INE9": "NOTHAT", "INE3": "C"}
    stored = {"INE1": "A", "INE2": "B"}
    explained, wrong = membership.compare_list(listed, stored, {"INE1", "INE2", "INE3"}, {"C"})
    assert {e["isin"] for e in explained} == {"DUM545A01024", "INE9", "INE3"}
    assert wrong == [{"isin": "INE2", "symbol": "B", "in": "store"}]
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
    summary, findings = sessions.check_sessions({D1, D2, D3}, set(), {D1, D2}, {D1}, set(), D1, D3)
    assert summary["calendar_sessions"] == 3
    codes = {f["code"]: f["items"] for f in findings}
    assert codes["session_without_bhavcopy"] == [D3.isoformat()]
    assert codes["session_not_stored"] == [D2.isoformat()]


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


def test_d7_etf_store_prices_are_unadjusted_before_the_compare() -> None:
    row = {
        "symbol": "NIFTYBEES",
        "date": D1,
        "open": 2_000_000,
        "high": 2_000_000,
        "low": 2_000_000,
        "close": 2_000_000,
        "factor": "0.1",
    }
    raw = {("NIFTYBEES", D1): [Decimal(2000)] * 4}
    assert series.compare_etf([row], raw) == (1, [], [])
    compared, wrong, missing = series.compare_etf(
        [row | {"close": 2_001_000}, row | {"date": D2}], raw
    )
    assert (compared, [w["field"] for w in wrong], len(missing)) == (1, ["close"], 1)
