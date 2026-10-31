"""The pure seams of the repo's operator, data and agent scripts (scripts/): argument and path
handling, parsers and the merge check's verdict, on hand-built inputs. Nothing here reaches the
network, a broker, herdr, git or a database."""

import importlib.util
import json
import sys
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

from app.hat.backtest.report import (
    DELISTED,
    END_OF_PERIOD,
    BacktestResult,
    FillRow,
    TradeRow,
    WatchRow,
)
from app.paths import PROJECT_ROOT


def _script(relative: str) -> ModuleType:
    """A script as a module, without putting its directory on sys.path."""
    path = PROJECT_ROOT / "scripts" / relative
    name = f"script_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


kotak = _script("data/kotak_5m.py")
n500 = _script("data/nifty500_membership.py")
merge = _script("agents/merge_check.py")
builder = _script("agents/builder.py")
secrets = _script("set_secrets.py")
fyers_login = _script("fyers_login.py")
results_probe = _script("research/s2_results_probe.py")
ep_b7 = _script("research/s2_ep_intraday_results.py")
ep_audit = _script("research/s2_ep_audit.py")
overlay = _script("data/overlay_etf.py")
truncation = _script("research/bt_truncation.py")
setups_audit = _script("research/s1_setups_audit.py")


# ---------------------------------------------------------------------------------------------
# kotak_5m


def test_kotak__five_years_ago() -> None:
    assert kotak.five_years_ago(date(2026, 10, 26)) == date(2021, 10, 27)
    assert kotak.five_years_ago(date(2024, 2, 29)) == date(2019, 3, 1)


def test_kotak__windows_cover_the_range_newest_first() -> None:
    got = list(kotak.windows(date(2026, 3, 31), date(2026, 1, 1)))
    assert got[0] == (date(2026, 3, 2), date(2026, 3, 31))
    assert got[-1][0] == date(2026, 1, 1)
    days = sum((end - start).days + 1 for start, end in got)
    assert days == (date(2026, 3, 31) - date(2026, 1, 1)).days + 1
    assert all((end - start).days < kotak.WINDOW_DAYS for start, end in got)


def test_kotak__read_members_keeps_stocks() -> None:
    lines = ["AAA,INE000A01011,EQ,id-a,A Ltd", "REIT,INE000B01011,BL,id-b,B", "", "CCC,X,BE,id-c"]
    assert [m["symbol"] for m in kotak.read_members(lines)] == ["AAA", "CCC"]


def _scrip(token: str, name: str, isin: str, group: str = "EQ") -> dict[str, str]:
    return {"pSymbol": token, "pSymbolName": name, "pISIN": isin, "pGroup": group}


def test_kotak__match_token_order() -> None:
    row = {"isin": "INE123A01016", "symbol": "OLD", "member_symbols": "OLD OLDER"}
    by_isin = [_scrip("1", "NEW", "INE123A01016")]
    assert kotak.match_token(row, by_isin) == ("1", "isin")
    same_issuer = [_scrip("2", "OLDER", "INE123A01024")]
    assert kotak.match_token(row, same_issuer) == ("2", "symbol:OLDER:INE123A01024")
    reused = [_scrip("3", "OLD", "INE999Z01011")]
    assert kotak.match_token(row, reused) is None
    renamed = [_scrip("4", "RENAMED", "INE123A01032"), _scrip("5", "RENAMED", "INE123A07011")]
    assert kotak.match_token(row, renamed) == ("4", "issuer:RENAMED:INE123A01032")


def test_kotak__redact_drops_token_fields() -> None:
    text = kotak.redact({"Error": "bad", "token": "s3cr3t", "sid": "x", "data": {"k": 1}})
    assert "s3cr3t" not in text
    assert "bad" in text


def test_kotak__work_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KOTAK_5M_DIR", raising=False)
    with pytest.raises(SystemExit):
        kotak.parse_args(["summary"])
    assert kotak.parse_args(["--work", "/w", "download", "--limit", "3"]).limit == 3


# ---------------------------------------------------------------------------------------------
# overlay_etf


def test_overlay__overlapping_window_files_give_one_candle_per_time_the_latest() -> None:
    older = [
        ["2026-10-26T00:00:00+05:30", 1, 1, 1, 1, 5],
        ["2026-10-27T00:00:00+05:30", 2, 2, 2, 2, 5],
    ]
    newer = [
        ["2026-10-27T00:00:00+05:30", 3, 3, 3, 3, 6],
        ["2026-10-28T00:00:00+05:30", 4, 4, 4, 4, 6],
    ]
    got = overlay.unique_candles([older, newer])
    assert [c[4] for c in got] == [1, 3, 4]


def test_kotak__topup_takes_its_session_range() -> None:
    args = kotak.parse_args(["--work", "/w", "topup", "--from", "2026-10-27", "--to", "2026-10-28"])
    assert (args.start, args.end) == (date(2026, 10, 27), date(2026, 10, 28))


def test_overlay__fixed_windows_are_anchored_and_cover_the_range() -> None:
    got = overlay.fixed_windows(date(2021, 2, 5), date(2021, 4, 10), 30, date(2021, 1, 1))
    assert got == [
        (date(2021, 2, 5), date(2021, 3, 1)),
        (date(2021, 3, 2), date(2021, 3, 31)),
        (date(2021, 4, 1), date(2021, 4, 10)),
    ]
    later = overlay.fixed_windows(date(2021, 2, 6), date(2021, 4, 30), 30, date(2021, 1, 1))
    assert later[1] == got[1]  # a rerun a day later asks for the same middle window


def _bar(day: date, close: float, volume: float = 100.0) -> object:
    return overlay.Bar(day, close, close, close, close, volume, "t")


def test_overlay__adjust_back_adjusts_before_each_ex_date() -> None:
    bars = [_bar(date(2019, 12, 18), 1292.54), _bar(date(2019, 12, 19), 130.20, 2000.0)]
    split = overlay.Split(date(2019, 12, 19), 0.1, "FV 10 to 1")
    (before, f0), (after, f1) = overlay.adjust(bars, [split])
    assert (before.close, before.volume, f0) == (pytest.approx(129.254), 1000.0, 0.1)
    assert (after.close, after.volume, f1) == (130.20, 2000.0, 1.0)
    two = overlay.adjust(bars[:1], [split, overlay.Split(date(2020, 1, 1), 0.5, "")])
    assert two[0][1] == pytest.approx(0.05)


def test_overlay__jumps_flag_split_sized_moves_only() -> None:
    closes = [
        (date(2019, 12, 18), 1292.54),
        (date(2019, 12, 19), 130.2),
        (date(2019, 12, 20), 120.0),
    ]
    assert [d for d, _ in overlay.jumps(closes)] == [date(2019, 12, 19)]


def test_overlay__dividends_land_on_the_next_session() -> None:
    sessions = [date(2024, 3, 8), date(2024, 3, 11), date(2024, 3, 12)]
    divs = {
        date(2024, 3, 8): 0.1,
        date(2024, 3, 9): 0.1,
        date(2024, 3, 10): 0.2,
        date(2024, 3, 13): 9,
    }
    assert overlay.dividends_by_session(sessions, divs) == {
        date(2024, 3, 8): 0.1,
        date(2024, 3, 11): pytest.approx(0.3),
    }


def test_overlay__total_return_reinvests_dividends() -> None:
    closes = [(date(2024, 1, 1), 1000.0), (date(2024, 1, 2), 1000.0), (date(2024, 1, 3), 999.0)]
    tr = overlay.total_return(closes, {date(2024, 1, 2): 0.2})
    assert [v for _, v in tr] == pytest.approx([100.0, 100.02, 100.02 * 999 / 1000])


def test_overlay__rate_proxy_deducts_the_expense_ratio_by_calendar_day() -> None:
    rate = [(date(2024, 1, 1), 2000.0), (date(2024, 1, 2), 2000.0), (date(2024, 1, 5), 2002.0)]
    got = overlay.rate_proxy(rate, 0.0073)
    assert got[1][1] == pytest.approx(100 * (1 - 0.0073) ** (1 / 365))
    assert got[2][1] == pytest.approx(got[1][1] * 1.001 * (1 - 0.0073) ** (3 / 365))
    assert overlay.rate_proxy(rate, 0.0)[-1][1] == pytest.approx(100.1)


def test_overlay__splice_chains_growth_and_tags_each_day() -> None:
    days = [date(2020, 3, 30), date(2020, 3, 31), date(2020, 4, 1), date(2020, 4, 2)]
    tr = overlay.splice(days, {date(2020, 4, 2): 1.002}, {date(2020, 3, 31): 1.001})
    assert [t for _, _, t in tr] == ["start", "proxy", "none", "dividend"]
    assert tr[-1][1] == pytest.approx(100 * 1.001 * 1.002)
    tags = [(d, t) for d, _, t in tr]
    assert overlay.ranges(tags, "proxy") == [(date(2020, 3, 31), date(2020, 3, 31))]
    assert overlay.ranges(tags, "dividend") == [(date(2020, 4, 2), date(2020, 4, 2))]


def test_overlay__coverage_lists_gaps_against_the_calendar() -> None:
    sessions = [date(2024, 1, d) for d in (1, 2, 3, 4, 5, 8, 9)]
    got = overlay.coverage([date(2024, 1, d) for d in (2, 3, 8, 9, 6)], sessions, date(2024, 1, 1))
    assert got["first"] == "2024-01-02"
    assert got["gaps"] == [["2024-01-04", "2024-01-05"]]
    assert got["missing"] == 2
    assert got["extra"] == ["2024-01-06"]


def test_overlay__parse_niftyindices_rows() -> None:
    tri = json.dumps(
        [
            {"Date": "09 Jan 2015", "TotalReturnsIndex": "10,906.56"},
            {"Date": "08 Jan 2015", "TotalReturnsIndex": "10840.84"},
            {"Date": "", "TotalReturnsIndex": "1"},
        ]
    ).encode()
    assert overlay.parse_niftyindices(tri, "Date", "TotalReturnsIndex") == [
        (date(2015, 1, 8), 10840.84),
        (date(2015, 1, 9), 10906.56),
    ]
    rate = json.dumps([{"HistoricalDate": "09 Jan 2015", "OPEN": "-", "CLOSE": "1368.49"}])
    assert overlay.parse_niftyindices(rate.encode(), "HistoricalDate", "CLOSE") == [
        (date(2015, 1, 9), 1368.49)
    ]
    with pytest.raises(ValueError):
        overlay.parse_niftyindices(b"<html>", "Date", "CLOSE")


def test_overlay__parse_nimf_dividends_and_year_chunks() -> None:
    row = {"DividendEffectiveDate": "2025-01-01T00:00:00", "DividendRetail": 0.15335083}
    got = overlay.parse_nimf_dividends(json.dumps({"FetchDividendDetailsResponse": [row]}).encode())
    assert got == [(date(2025, 1, 1), 0.15335083)]
    assert overlay.parse_nimf_dividends(b'{"Return_Message": "No data found"}') == []
    assert overlay.year_chunks(date(2010, 1, 1), date(2026, 10, 27), 5)[-1] == (
        date(2025, 1, 1),
        date(2026, 10, 27),
    )


def test_overlay__dividend_sessions_need_every_day_since_the_last_session_covered() -> None:
    s = [date(2020, 3, 27), date(2020, 3, 31), date(2020, 4, 1), date(2020, 4, 3)]
    assert overlay.dividend_sessions(s, date(2020, 4, 1), date(2020, 4, 3)) == {
        date(2020, 4, 1),
        date(2020, 4, 3),
    }
    assert overlay.dividend_sessions(s, date(2020, 4, 2), date(2020, 4, 2)) == set()


def test_overlay__index_archive_rows_fill_the_gaps() -> None:
    lines = [
        "Index Name,Index Date,Open,High,Low,Closing Index Value,Points Change",
        "Nifty 1D Rate Index,10-02-2020,-,-,-,1870.84,0.75",
        "Nifty 50,10-02-2020,1,1,1,12031.5,1",
        "Nifty 1D Rate Index,11-02-2020,-,-,-,-,0",
    ]
    rows = overlay.parse_index_archive(lines, "NIFTY 1D RATE INDEX")
    assert rows == [(date(2020, 2, 10), 1870.84)]
    primary = [(date(2020, 2, 7), 1870.0), (date(2020, 2, 11), 1871.6)]
    other = [*rows, (date(2020, 2, 11), 1871.7)]
    merged, added, diffs = overlay.fill(primary, other)
    assert [d for d, _ in merged] == [date(2020, 2, 7), date(2020, 2, 10), date(2020, 2, 11)]
    assert merged[-1][1] == 1871.6
    assert added == [date(2020, 2, 10)]
    assert diffs == [(date(2020, 2, 11), 1871.6, 1871.7)]


# ---------------------------------------------------------------------------------------------
# nifty500_membership


def test_n500__parse_date_formats() -> None:
    assert n500.parse_date("w.e.f. March 16, 2017") == "2017-03-16"
    assert n500.parse_date("from 5th September 2017") == "2017-09-05"
    assert n500.parse_date("no date here") is None


NOTICE = """Replacements in indices
The following changes are effective from March 31, 2025
(1) NIFTY 500
The following companies are being excluded:
Sr. No. Company Name Symbol
1 Old Company Ltd. OLDCO
The following companies are being included:
1 New Company Ltd. NEWCO
2 Another Ltd. ANOTHER
(2) NIFTY 50
1 Something Else ELSE
"""


def test_n500__parse_notice_reads_the_nifty_500_section() -> None:
    effective, sections = n500.parse_notice(NOTICE)
    assert effective == ["2025-03-31"]
    assert len(sections) == 1
    assert sections[0]["excluded"] == [["Old Company Ltd.", "OLDCO"]]
    assert [s for _, s in sections[0]["included"]] == ["NEWCO", "ANOTHER"]


def _parsed(file: str, day: str, out: list[str], inc: list[str]) -> dict[str, object]:
    section = {"excluded": [["", s] for s in out], "included": [["", s] for s in inc]}
    return {"file": file, "effective": [day], "sections": [section]}


def test_n500__notice_events_apply_overrides() -> None:
    parsed = [
        _parsed("a.pdf", "2020-01-10", ["X"], ["Y"]),
        _parsed("b.pdf", "2020-02-10", ["P", "Q"], ["R", "S"]),
        _parsed("c.pdf", "2020-03-10", ["Q"], ["S"]),
        _parsed("old.pdf", "2015-01-10", ["Z"], ["W"]),
    ]
    overrides = {
        "_doc": "ignored",
        "a.pdf": {"skip": True},
        "c.pdf": {"date": "2020-03-20", "drop_from": "b.pdf"},
        "_extra": [{"date": "2020-04-01", "file": "x.pdf", "excluded": ["T"]}],
    }
    parsed.append(_parsed("later.pdf", "2026-11-01", ["L"], ["M"]))
    events = n500.notice_events(parsed, overrides, date(2016, 1, 1), date(2026, 10, 25))
    assert sorted(events) == [
        (date(2020, 2, 10), "b.pdf", ["P"], ["R"]),
        (date(2020, 3, 20), "c.pdf", ["Q"], ["S"]),
        (date(2020, 4, 1), "x.pdf", ["T"], []),
    ]


def _reference() -> object:
    return n500.Reference(
        isin={"old": "INE1", "new": "INE2", "other": "INE3"},
        lineage=[("old", "new", "2020-06-01")],
        spans={
            "ABC": [
                (date(2015, 1, 1), date(2020, 5, 29), "old"),
                (date(2023, 1, 1), date(2026, 1, 1), "other"),
            ],
            "ABCNEW": [(date(2020, 6, 1), date(2026, 1, 1), "new")],
        },
        history={"new": [(date(2020, 6, 1), None, "ABCNEW")]},
        alias={"ALIASED": "ISIN:INE3", "DUMMY": "PSEUDO:DUM1"},
        first_bar=date(2015, 1, 1),
    )


def test_n500__resolve_and_key() -> None:
    ref = _reference()
    assert ref.resolve("ABC", date(2020, 5, 30), "out") == "old"
    assert ref.resolve("ABC", date(2024, 1, 1), "in") == "other"
    assert ref.resolve("ABC", date(2021, 8, 1), "in") is None  # 400+ days from either span
    assert ref.resolve("ALIASED", date(2024, 1, 1), "in") == "other"
    assert ref.resolve("DUMMY", date(2024, 1, 1), "in") == "PSEUDO:DUM1"
    assert ref.key("old") == "new"
    assert ref.symbol_on("new", date(2021, 1, 1), "X") == "ABCNEW"


def test_n500__split_by_isin_at_lineage_dates() -> None:
    ref = _reference()
    assert n500.split_by_isin(ref, "new", date(2016, 1, 1), None) == [
        ("old", date(2016, 1, 1), date(2020, 6, 1)),
        ("new", date(2020, 6, 1), None),
    ]
    assert n500.split_by_isin(ref, "new", date(2021, 1, 1), date(2022, 1, 1)) == [
        ("new", date(2021, 1, 1), date(2022, 1, 1)),
    ]


def test_n500__wanted_notice() -> None:
    assert n500.wanted_notice("Oct 23, 2026", "Replacements in indices")
    assert not n500.wanted_notice("Oct 23, 2026", "Launch of Nifty ETF index")
    assert n500.wanted_notice("Jan 05, 2010", "Replacements in indices")
    assert not n500.wanted_notice("Dec 28, 2009", "Replacements in indices")


def test_n500__resolve_before_the_bars_and_from_archived_lists() -> None:
    """Before HAT's first bar the distance is measured from it; a symbol no span resolves takes
    the ISIN the nearest archived list gives it."""
    ref = _reference()
    assert ref.resolve("ABC", date(2012, 1, 1), "in") == "old"  # 1096 days, clamped to 0
    ref.listed = {"GONE": [(date(2010, 1, 2), "INE9")], "ABCX": [(date(2010, 1, 2), "INE3")]}
    assert ref.resolve("GONE", date(2010, 5, 1), "out") == "PSEUDO:INE9"
    assert ref.resolve("ABCX", date(2010, 5, 1), "in") == "other"
    assert ref.resolve("NEVER", date(2010, 5, 1), "in") is None
    # a symbol-history interval from 1900 is a span, never the first bar: the clamp holds
    spans = {**ref.spans, "XYZ": [(date(1900, 1, 1), date(2026, 1, 1), "other")]}
    early = n500.Reference(ref.isin, ref.lineage, spans, ref.history, first_bar=ref.first_bar)
    assert early.resolve("ABC", date(2012, 1, 1), "in") == "old"


def test_n500__archived_lists(tmp_path: Path) -> None:
    path = tmp_path / "20100102131654_cnx500.csv"
    path.write_text(
        "\nConstituents list of S&P CNX 500\n\n"
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "ABC Ltd.,BANKS,ABC,EQ,INE1\n"
        "No Isin Ltd.,POWER,NOISIN,EQ,\n"
    )
    assert n500.list_day(path) == date(2010, 1, 2)
    assert n500.list_day(Path("2015-03-25.csv")) == date(2015, 3, 25)
    rows = n500.read_list(path)
    assert [(r["Symbol"], r["ISIN Code"], r["Industry"]) for r in rows] == [
        ("ABC", "INE1", "BANKS")
    ]
    ref = _reference()
    gone = {"Symbol": "GONE", "ISIN Code": "INE9"}
    assert n500.list_members([*rows, gone], ref, date(2010, 1, 2)) == {
        "new": "ABC",  # INE1 is `old`, keyed by its chain's end
        "PSEUDO:INE9": "GONE",
    }


def test_n500__clip_cuts_the_history_at_its_start() -> None:
    from app.hat.data.index_membership import Member

    start = date(2011, 6, 22)
    members = [
        Member("A", "A", date(2010, 1, 2), date(2011, 1, 1), "x.pdf;y.pdf"),  # gone by start
        Member("B", "B", date(2010, 4, 8), date(2012, 3, 7), "in.pdf;out.pdf"),
        Member("C", "C", date(2010, 1, 2), None, "in.pdf"),
        Member("D", "D", date(2013, 1, 1), None, "d.pdf"),
    ]
    assert n500.clip(members, start, "today.csv") == [
        Member("B", "B", start, date(2012, 3, 7), "out.pdf"),
        Member("C", "C", start, None, "today.csv"),
        members[3],
    ]


# ---------------------------------------------------------------------------------------------
# set_secrets, fyers_login


def test_set_secrets__updated_env() -> None:
    text = "A=1\nexport KEY=old\n# note\nKEY=dup\n"
    assert secrets.updated_env(text, {"KEY": "new", "NEW": "v"}) == "A=1\nKEY=new\n# note\nNEW=v\n"


def test_set_secrets__request_allowed() -> None:
    own = {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765"}
    allowed = secrets.request_allowed
    assert allowed(own, "tok", "tok", 8765, post=True)
    assert allowed({"Host": "127.0.0.1:8765"}, "", "tok", 8765, post=False)
    assert not allowed(own, "other", "tok", 8765, post=True)
    assert not allowed({**own, "Origin": "https://evil.test"}, "tok", "tok", 8765, post=True)
    assert not allowed({"Host": "127.0.0.1:8765"}, "tok", "tok", 8765, post=True)
    assert not allowed({**own, "Host": "evil.test:8765"}, "", "tok", 8765, post=False)


def test_set_secrets__same_origin() -> None:
    host = "127.0.0.1:8765"
    assert secrets.same_origin("http://" + host, host)
    for other in ("https://" + host, "http://127.0.0.1:8766", "http://evil.test", "", "null"):
        assert not secrets.same_origin(other, host)


def test_fyers_login__auth_code() -> None:
    tabs = [{"url": "https://example.test/login"}, {"url": "http://127.0.0.1/cb?auth_code=abc&s=1"}]
    assert fyers_login.auth_code(tabs) == "abc"
    assert fyers_login.auth_code([{"url": ""}, {}]) is None


# ---------------------------------------------------------------------------------------------
# merge_check


def _facts(**kw: object) -> object:
    base = {"pr": 7, "head": "abcdef1234", "merge_state": "CLEAN", "sonar": "OK"}
    return merge.Facts(**{**base, **kw})


def test_merge__verdict_ok() -> None:
    assert merge.verdict(_facts(reported="abcdef1")) == (True, "MERGE OK #7 abcdef1")


def test_merge__verdict_lists_every_reason() -> None:
    ok, line = merge.verdict(
        _facts(
            reported="1234567",
            merge_state="BEHIND",
            check_problems=["ci fail"],
            sonar="ERROR",
            open_threads=["a.py (last: bot)"],
            secrets=["x.py:AWS key"],
        )
    )
    assert not ok
    assert line.startswith("NO MERGE #7 abcdef1: head abcdef1 is not the reported 1234567;")
    for part in ("BEHIND", "checks: ci fail", "sonar gate ERROR", "1 unanswered", "AWS key"):
        assert part in line


def test_merge__review_bot_pending_does_not_block() -> None:
    assert merge.verdict(_facts(review_bot="pending"))[0]


def test_merge__check_problems() -> None:
    checks = [
        {"name": "ci", "bucket": "pass"},
        {"name": "tla", "bucket": "pending"},
        {"name": "skip", "bucket": "skipping"},
        {"name": "sonar", "bucket": "fail"},
    ]
    assert merge.check_problems(checks) == ["tla pending", "sonar fail"]
    assert merge.check_problems([]) == ["no checks"]


def test_merge__review_bot_status() -> None:
    comments = [{"author": {"login": "codeant-ai"}, "body": "CodeAnt AI is reviewing your PR."}]
    assert merge.review_bot_status(comments) == "pending"
    comments.append(
        {"author": {"login": "codeant-ai"}, "body": "CodeAnt AI finished. Reviewed your PR."}
    )
    assert merge.review_bot_status(comments) == "done"


def _thread(resolved: bool, *logins: str) -> dict[str, object]:
    nodes = [{"author": {"login": login}} for login in logins]
    return {"isResolved": resolved, "path": "f.py", "comments": {"nodes": nodes[-1:]}}


def test_merge__open_threads() -> None:
    def page(*threads: dict[str, object]) -> dict[str, object]:
        return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": threads}}}}}

    pages = [
        page(_thread(True, "bot"), _thread(False, "bot", "owner")),
        page(_thread(False, "bot")),
    ]
    assert merge.open_threads(pages, "owner") == ["f.py (last: bot)"]


def test_merge__secret_hits_on_added_lines_only() -> None:
    aws = "AKIA" + "ABCDEFGHIJKLMNOP"
    diff = "\n".join(
        [
            "+++ b/app/x.py",
            f"+key = '{aws}'",
            f"-old = '{aws}'",
            "+password = os.environ.get('DB_PASSWORD')",
            "+++ b/.env.example",
            "+KITE_API_SECRET=",
            "+HAT_TOKEN_PATH=${HOME}/token",
            "+++ b/deploy.env",
            "+KOTAK_NEO_MPIN=12345678",
            "+api_key: '" + "a" * 16 + "'",
            "+++ b/keys.env",
            "+KOTAK_NEO_CONSUMER_KEY=" + "x" * 20,
        ]
    )
    assert merge.secret_hits(diff) == [
        "app/x.py:AWS key",
        "deploy.env:env secret",
        "deploy.env:quoted secret",
        "keys.env:env secret",
    ]


# ---------------------------------------------------------------------------------------------
# builder


def test_builder__branch_of() -> None:
    assert builder.branch_of("repo-tooling") == "hat/repo-tooling"
    assert builder.branch_of("pr.75e_fix") == "hat/pr.75e_fix"
    for bad in ("", "-x", "a b", "a/b", "a;rm", "task.", "a..b", "x.lock", "a--b"):
        with pytest.raises(ValueError, match="bad slug"):
            builder.branch_of(bad)


def test_builder__default_worktrees() -> None:
    common = Path("/w/main/.git")
    assert builder.default_worktrees(common, {}) == Path("/w/wt/hat")
    assert builder.default_worktrees(common, {"HAT_WORKTREES": "/x"}) == Path("/x")


def test_builder__herdr_json() -> None:
    assert builder.pane_of({"result": {"root_pane": {"pane_id": "w1:p9"}}}) == "w1:p9"
    agents = {"result": {"agents": [{"agent": "claude"}, {"name": "s", "tab_id": "w1:t9"}]}}
    assert builder.find_agent(agents, "s") == {"name": "s", "tab_id": "w1:t9"}
    assert builder.find_agent(agents, "x") is None
    assert builder.find_tab({"result": {"tabs": [{"label": "s", "tab_id": "t"}]}}, "s") == "t"


def test_builder__open_prs() -> None:
    prs = [{"number": 1, "state": "MERGED"}, {"number": 2, "state": "OPEN"}]
    assert builder.open_prs(prs) == [2]


hook = builder.hook
PAYLOADS = (PROJECT_ROOT / "backend/tests/fixtures/claude_hook_payloads.json").read_text()


def _replay(state: dict[str, object] | None, *payloads: dict[str, object]) -> dict[str, object]:
    state = state or hook.initial_state()
    for p in payloads:
        state = hook.apply(state, p, {"CLAUDE_PID": "7", "TMUX_PANE": "%4"}, 1.0) or state
    return state


def test_builder__choose_backend() -> None:
    assert builder.choose_backend({"HERDR_ENV": "1", "TMUX": "/t"}, None) == "herdr"
    assert builder.choose_backend({"TMUX": "/t"}, None) == "tmux"
    assert builder.choose_backend({}, "tmux") == "tmux"
    with pytest.raises(ValueError, match="not inside herdr or tmux"):
        builder.choose_backend({"HERDR_ENV": "0"}, None)


def test_builder__parse_duration() -> None:
    assert [builder.parse_duration(t) for t in ("90", "90s", "45m", "2h")] == [90, 90, 2700, 7200]
    with pytest.raises(ValueError, match="bad duration"):
        builder.parse_duration("1d")


def test_builder__with_hooks_is_idempotent_and_keeps_other_entries() -> None:
    new = "python3 /new/agent_hook.py"
    other = {"hooks": [{"type": "command", "command": "other.sh"}]}
    old = {"hooks": [{"type": "command", "command": "python3 /old/agent_hook.py", "timeout": 10}]}
    settings = {"model": "x", "hooks": {"Stop": [other], "Notification": [old]}}
    once = builder.with_hooks(settings, new)
    assert once["model"] == "x"
    assert [g["hooks"][0]["command"] for g in once["hooks"]["Stop"]] == ["other.sh", new]
    assert once["hooks"]["Notification"] == [{"hooks": [{**old["hooks"][0], "command": new}]}]
    assert set(once["hooks"]) == set(builder.HOOK_EVENTS)
    assert builder.with_hooks(once, new) == once
    assert settings["hooks"]["Notification"] == [old]  # input untouched


def test_hook__slug_of() -> None:
    wt = Path("/w/wt/hat")
    assert hook.slug_of({"BUILDER_SLUG": "a-b"}, "/elsewhere", wt) == "a-b"
    assert hook.slug_of({}, "/w/wt/hat/a-b/backend", wt) == "a-b"
    assert hook.slug_of({}, "/w/main", wt) is None
    assert hook.slug_of({}, "/w/wt/hat", wt) is None
    assert hook.slug_of({"BUILDER_SLUG": "a b"}, "/w/main", wt) is None


def test_hook__verdict_of() -> None:
    assert hook.verdict_of("done\nREADY TO MERGE #12 abc1234\n") == "READY TO MERGE #12 abc1234"
    assert hook.verdict_of("x\n● **BLOCKED #3: CI red**") == "BLOCKED #3: CI red"
    assert hook.verdict_of("`OWNER DECISION: keep X?`\nok") == "OWNER DECISION: keep X?"
    assert hook.verdict_of("I will report READY TO MERGE #1 abc1234 later") is None
    assert hook.verdict_of("READY TO MERGE #1 zzzzzzz") is None
    assert hook.verdict_of("READY TO MERGE #1") is None  # the sha is required
    assert hook.verdict_of("") is None


def test_hook__replays_real_payloads() -> None:
    p = json.loads(PAYLOADS)
    events = [x["hook_event_name"] for x in p]
    assert events[:3] == ["SessionStart", "UserPromptSubmit", "Stop"]
    s = _replay(None, *p[:3])
    assert (s["turn"], s["state"], s["verdict"]) == (1, "idle", "OWNER DECISION: sample question")
    assert (s["pid"], s["pane"], s["session_id"]) == (7, "%4", p[0]["session_id"])
    s = _replay(None, *p[:5])  # a turn that stopped with a background task running
    assert (s["turn"], s["state"], s["results"]["2"]) == (
        2,
        "waiting-on-background-task",
        "background-task",
    )
    s = _replay(None, *p[:7])  # the task's completion notice continues turn 2
    assert (s["turn"], s["state"], s["results"]["2"]) == (2, "idle", "idle no-verdict")
    s = _replay(None, *p[:9])  # a permission dialog
    assert (s["turn"], s["state"], s["results"]["3"]) == (3, "needs-input", "needs-input")
    s = _replay(None, *p)  # ends with an idle_prompt notice, which changes nothing
    assert p[-1]["notification_type"] == "idle_prompt"
    assert hook.apply(_replay(None, *p[:-1]), p[-1], {}, 9.0) is None
    assert (s["turn"], s["state"], s["results"]["4"]) == (4, "idle", "idle no-verdict")
    assert s["verdict"] == "OWNER DECISION: sample question"  # the last verdict stays


def test_hook__verdict_with_background_work_is_not_final() -> None:
    go = {"hook_event_name": "UserPromptSubmit", "prompt": "go"}
    ready = "READY TO MERGE #5 abc1234"
    stop = {"hook_event_name": "Stop", "last_assistant_message": ready}
    s = _replay(None, go, {**stop, "background_tasks": [{"status": "running"}]})
    assert (s["state"], s["results"]["1"], s.get("verdict")) == (
        "waiting-on-background-task",
        "background-task",
        None,
    )
    notice = {"hook_event_name": "UserPromptSubmit", "prompt": "<task-notification>x"}
    s = _replay(s, notice, {**stop, "background_tasks": []})
    assert (s["turn"], s["state"], s["results"]["1"], s["verdict"]) == (1, "idle", ready, ready)


def test_hook__compaction_keeps_the_turn_working() -> None:
    s = _replay(None, {"hook_event_name": "UserPromptSubmit", "prompt": "go"})
    s = _replay(s, {"hook_event_name": "SessionStart", "source": "compact"})
    assert (s["turn"], s["state"]) == (1, "working")


def test_hook__wait_answer() -> None:
    done = {
        "turn": 2,
        "seq": 5,
        "state": "idle",
        "results": {"1": "BLOCKED #1: x", "2": "idle no-verdict"},
    }
    assert hook.wait_answer(done, 2, since=5) == "idle no-verdict"  # ended already: at once
    assert hook.wait_answer(done, 1, since=5) == "BLOCKED #1: x"
    assert hook.wait_answer({**done, "results": {}}, 2, since=5) is None  # still working
    bg = {**done, "state": "waiting-on-background-task", "results": {"2": "background-task"}}
    assert hook.wait_answer(bg, 2, since=5) is None  # not news when the wait began
    assert hook.wait_answer(bg, 2, since=4) == "background-task"
    asking = {**done, "state": "needs-input", "results": {"2": "needs-input"}}
    assert hook.wait_answer(asking, 2, since=5) == "needs-input"  # a dialog is up: at once
    answered = hook.answered(asking, 9.0)
    assert answered is not None
    assert (answered["state"], answered["seq"]) == ("working", 6)
    assert hook.wait_answer(answered, 2, since=6) is None
    assert hook.answered(done, 9.0) is None


def test_hook__style_of() -> None:
    idle = {"turn": 1, "state": "idle", "results": {"1": "READY TO MERGE #1 abc1234"}}
    assert hook.style_of(idle) == "fg=green"
    assert hook.style_of({**idle, "results": {"1": "idle no-verdict"}}) == "fg=yellow"
    assert hook.style_of({**idle, "state": "needs-input"}) == "fg=red"


# ---------------------------------------------------------------------------------------------
# s2_results_probe (recorded NSE answers, trimmed, under fixtures/nse_results)

NSE_RESULTS = PROJECT_ROOT / "backend" / "tests" / "fixtures" / "nse_results"


def _nse(name: str) -> bytes:
    return (NSE_RESULTS / name).read_bytes()


def test_results_probe__nse_time_formats() -> None:
    ist = results_probe.IST
    assert results_probe.nse_time("16-Jan-2025 20:20:21") == datetime(
        2025, 1, 16, 20, 20, 21, tzinfo=ist
    )
    assert results_probe.nse_time("16-Jan-2025 20:20") == datetime(2025, 1, 16, 20, 20, tzinfo=ist)
    assert results_probe.nse_time("30-JUN-2026") == datetime(2026, 6, 30, tzinfo=ist)
    assert results_probe.nse_time("-") is None
    assert results_probe.nse_time(None) is None


def test_results_probe__results_listing() -> None:
    xbrl, gap, old = results_probe.parse_results_listing(_nse("results_listing.json"))
    assert (xbrl.period_end, xbrl.basis, xbrl.source) == (date(2024, 12, 31), "standalone", "reg33")
    assert xbrl.filed_at == datetime(2025, 1, 16, 20, 20, 21, tzinfo=results_probe.IST)
    assert xbrl.filing_date_field == "broadCastDate"
    assert xbrl.xbrl_url is not None
    assert xbrl.xbrl_url.endswith(".xml")
    assert (gap.period_end, gap.xbrl_url, gap.detail_url) == (date(2018, 3, 31), None, None)
    assert old.xbrl_url is None
    assert old.detail_url is not None
    assert "financial_results" in old.detail_url


def test_results_probe__insurance_listing_reads_period_end() -> None:
    (row,) = results_probe.parse_results_listing(_nse("insurance_listing.json"), "insurance")
    assert (row.period_end, row.basis, row.source) == (
        date(2024, 12, 31),
        "standalone",
        "insurance",
    )
    assert "Life-Insurance" in row.listing_format


def test_results_probe__integrated_listing_keeps_financials() -> None:
    rows, total = results_probe.parse_integrated_listing(_nse("integrated_listing.json"))
    assert total == 19
    assert [(r.period_end, r.basis) for r in rows] == [
        (date(2026, 6, 30), "consolidated"),
        (date(2026, 6, 30), "standalone"),
    ]
    assert rows[0].filing_date_field == "broadcast_Date"
    assert rows[0].filed_at == datetime(2026, 7, 17, 19, 50, 3, tzinfo=results_probe.IST)


@pytest.mark.parametrize(
    ("name", "kind", "basis"),
    [
        ("indas", "reg33_indas", "standalone"),
        ("banking", "reg33_banking", "consolidated"),
        ("nbfc", "reg33_nbfc", "consolidated"),
        ("non_indas", "reg33_non_indas", "standalone"),
        ("insurance", "reg33_insurance", "standalone"),
        ("integrated_indas", "integrated_indas", "consolidated"),
        ("integrated_li", "integrated_li", "consolidated"),
    ],
)
def test_results_probe__classify_xbrl(name: str, kind: str, basis: str) -> None:
    found = results_probe.classify_xbrl(_nse(f"xbrl_{name}.xml"))
    assert (found.format, found.basis) == (kind, basis)
    assert found.schema_ref.endswith(".xsd")


def test_results_probe__classify_not_xbrl() -> None:
    assert results_probe.classify_xbrl(b"<html>blocked</html>").format == "not_xbrl"


def test_results_probe__detail_page_figures() -> None:
    assert results_probe.detail_has_figures(_nse("detail_page.html"))
    assert not results_probe.detail_has_figures(b"<html></html>")


def test_results_probe__results_announcement_is_the_pdf_one() -> None:
    items = results_probe.parse_announcements(_nse("announcements.json"))
    ist = results_probe.IST
    match, rule = results_probe.results_announcement(items)
    assert match is not None
    assert (match.at, rule) == (datetime(2025, 1, 16, 19, 25, 42, tzinfo=ist), "result_updates")
    filed = datetime(2025, 1, 16, 20, 15, 20, tzinfo=ist)
    assert results_probe.seconds_between(filed, match.at) == 2978
    board = results_probe.Announcement(
        datetime(2025, 1, 16, 19, 0, tzinfo=ist), "Outcome of Board Meeting", "held today", ""
    )
    others = [i for i in items if i.desc != "Financial Result Updates"]  # Integrated Filing etc.
    assert results_probe.results_announcement(others) == (None, "")
    assert results_probe.results_announcement([*others, board]) == (board, "board_outcome")


def test_results_probe__xbrl_name_stamp_and_quarters() -> None:
    url = "https://x/INTEGRATED_FILING_INDAS_1695741_17072026075004_WEB.xml"
    assert results_probe.xbrl_name_stamp(url) == "2026-07-17 07:50:04"
    assert results_probe.xbrl_name_stamp("https://x/corporate/xbrl/-") == ""
    stamp = results_probe.xbrl_stamp_at(url)
    assert stamp == datetime(2026, 7, 17, 7, 50, 4, tzinfo=results_probe.IST)
    assert results_probe.quarters_in(2025, date(2026, 10, 28)) == 4
    assert results_probe.quarters_in(2026, date(2026, 10, 28)) == 2  # Sep 2026 due mid-Nov
    assert results_probe.summary([30, -10, 50]) == {"n": 3, "median": 30, "min": -10, "max": 50}


# ---------------------------------------------------------------------------------------------
# s2_ep_intraday_results (B7) and s2_ep_audit (C4)

_IST = ep_b7.IST


def _filing(at: datetime, period: date, sales: str | None = "0.3") -> object:
    from decimal import Decimal

    return ep_b7.ResultEvent(at, period, None if sales is None else Decimal(sales))


def test_ep_b7__public_bucket() -> None:
    times = [
        datetime(2024, 5, 3, h, m, tzinfo=_IST) for h, m in ((9, 14), (9, 15), (15, 29), (15, 30))
    ]
    buckets = ["pre_open", "market", "market", "post_close"]
    assert [ep_b7.public_bucket(t) for t in times] == buckets


def test_ep_b7__first_filing_of_each_quarter() -> None:
    q1, q2 = date(2024, 3, 31), date(2024, 6, 30)
    first = _filing(datetime(2024, 5, 3, 10, tzinfo=_IST), q1)
    revised = _filing(datetime(2024, 5, 9, 10, tzinfo=_IST), q1, "0.9")
    later = _filing(datetime(2024, 8, 1, 10, tzinfo=_IST), q2)
    assert ep_b7.first_filings([later, revised, first]) == [first, later]


def test_ep_b7__growth_price_and_moves() -> None:
    from decimal import Decimal

    params = ep_b7.EarningsCatalystParams()
    assert (
        ep_b7.growth_group(
            _filing(datetime(2024, 5, 3, tzinfo=_IST), date(2024, 3, 31), "0.6"), params
        )
        == "pass"
    )
    assert (
        ep_b7.growth_group(
            _filing(datetime(2024, 5, 3, tzinfo=_IST), date(2024, 3, 31), "0.1"), params
        )
        == "fail"
    )
    assert (
        ep_b7.growth_group(
            _filing(datetime(2024, 5, 3, tzinfo=_IST), date(2024, 3, 31), None), params
        )
        == "undefined"
    )
    start = datetime(2024, 5, 3, 9, 15, tzinfo=_IST)
    bars = [
        ep_b7.Bar(
            "X",
            start.replace(minute=15 + 5 * i),
            Decimal(100 + i),
            Decimal(101 + i),
            Decimal(99),
            Decimal(100 + i),
            1,
        )
        for i in range(3)
    ]
    assert ep_b7.price_after(bars, start.replace(minute=21)) == Decimal(102)
    assert ep_b7.price_after(bars, start.replace(minute=20)) == Decimal(101)
    assert ep_b7.price_after(bars, start.replace(minute=40)) is None
    assert ep_b7.move_pct(Decimal(100), Decimal(110)) == pytest.approx(10.0)
    assert ep_b7.move_pct(None, Decimal(1)) is None
    days = [date(2024, 5, d) for d in (2, 3, 6, 7)]
    assert ep_b7.later_day(days, date(2024, 5, 3), 2) == date(2024, 5, 7)
    assert ep_b7.later_day(days, date(2024, 5, 3), 5) is None
    assert ep_b7.later_day(days, date(2024, 5, 4), 1) is None


def test_ep_b7__summary() -> None:
    got = ep_b7.summary([-2.0, 1.0, 3.0, 6.0])
    assert got["n"] == 4
    assert got["mean"] == 2.0
    assert got["median"] == 2.0
    assert got["up_pct"] == 75.0
    assert ep_b7.summary([])["mean"] is None
    assert ep_b7.summary([5.0])["p25"] == 5.0


_TRADE = {
    "playbook": "episodic_pivot",
    "symbol": "ABC",
    "instrument_id": "i1",
    "entry_date": "2023-05-03",
    "entry_price": "100",
    "initial_stop": "95",
    "exit_date": "2023-05-10",
    "exit_price": "110",
    "r_net": "0.5",
    "gap_pct": "12.5",
    "trigger_time": "09:35",
    "catalyst_type": "earnings",
}


def test_ep_audit__entry_cases_join_legs() -> None:
    rows = [
        {**_TRADE, "trade_id": "7", "leg": "1", "exit_price": "104", "exit_date": "2023-05-06"},
        {**_TRADE, "trade_id": "7", "leg": "2", "r_net": "1.25"},
        {**_TRADE, "trade_id": "8", "leg": "1", "playbook": "breakout"},
    ]
    (case,) = ep_audit.entry_cases(rows)
    assert (case.entry, case.stop, case.exit, case.exit_date) == (
        100.0,
        95.0,
        110.0,
        date(2023, 5, 10),
    )
    assert case.r == 1.75
    assert case.gap_pct == "12.5"
    assert case.trigger_time == "09:35"


def test_ep_audit__near_miss_is_after_the_setup_and_before_the_fill() -> None:
    gates = (
        "gap_scan",
        "gap_up",
        "neglect",
        "early_volume",
        "price_breaks_level",
        "max_chase",
        "filled",
    )
    setup = ("neglect",)
    for failed, near in (
        ("neglect", False),
        ("gap_up", False),
        ("early_volume", True),
        ("max_chase", True),
        ("filled", False),
        ("", False),
    ):
        assert ep_audit.near_miss({"failed_gate": failed}, gates, setup) is near, failed


def test_ep_audit__sample_is_seeded_and_bounded() -> None:
    cases = [ep_audit.Case("entry", f"S{i}", date(2023, 1, 1), *([""] * 6)) for i in range(30)]
    first = ep_audit.sample(cases, 20, 7)
    assert len(first) == 20
    assert first == ep_audit.sample(cases, 20, 7)
    assert ep_audit.sample(cases[:3], 20, 7) == cases[:3]


def test_ep_audit__sma_and_opening_range() -> None:
    from decimal import Decimal

    assert ep_audit.sma([1.0, 2.0, 3.0, 4.0], 2) == [None, 1.5, 2.5, 3.5]
    bar = ep_audit.Bar("X", datetime(2024, 5, 3, 9, 15, tzinfo=_IST), *(Decimal(5),) * 4, 1)
    assert ep_audit.opening_range_high([bar]) == 5.0
    assert ep_audit.opening_range_high([]) is None


def test_ep_audit__daily_span() -> None:
    days = [date(2023, 1, d) for d in range(2, 31)]
    until, count = ep_audit.daily_span(days, date(2023, 1, 3), date(2023, 1, 5))
    assert until == days[3 + ep_audit.DAILY_AFTER]
    assert count == ep_audit.DAILY_BEFORE + max(ep_audit.SMAS) + 2 + ep_audit.DAILY_AFTER
    assert ep_audit.daily_span(days, date(2023, 1, 30), date(2023, 1, 30))[0] == days[-1]


def test_ep_b7__split_filings() -> None:
    session = date(2024, 5, 3)
    market = _filing(datetime(2024, 5, 3, 11, tzinfo=_IST), date(2024, 3, 31))
    evening = _filing(datetime(2024, 5, 3, 18, tzinfo=_IST), date(2023, 12, 31))
    outside = _filing(datetime(2025, 1, 2, 11, tzinfo=_IST), date(2024, 9, 30))
    plain, by_day = ep_b7.split_filings(
        {"a": [market, evening, outside]}, {session}, date(2024, 1, 1), date(2024, 12, 31)
    )
    assert plain == [("a", evening)]
    assert by_day == {session: [("a", market)]}


def test_overlay__latest_ingest_keeps_one_row_per_symbol_and_day() -> None:
    old = "0190a000-0000-7000-8000-000000000000"
    new = "0190b000-0000-7000-8000-000000000000"
    rows = [
        {"symbol": "NIFTYBEES", "date": "2024-01-02", "close": 1.0, "ingest_run_id": new},
        {"symbol": "NIFTYBEES", "date": "2024-01-02", "close": 9.0, "ingest_run_id": old},
        {"symbol": "NIFTYBEES", "date": "2024-01-03", "close": 2.0, "ingest_run_id": old},
        {"symbol": "GOLDBEES", "date": "2024-01-02", "close": 3.0, "ingest_run_id": old},
    ]
    kept = overlay.latest_ingest(rows)
    assert [(r["symbol"], r["date"], r["close"]) for r in kept] == [
        ("NIFTYBEES", "2024-01-02", 1.0),
        ("NIFTYBEES", "2024-01-03", 2.0),
        ("GOLDBEES", "2024-01-02", 3.0),
    ]


# ---------------------------------------------------------------------------------------------
# bt_truncation

_UUID = uuid.UUID(int=1)


def _trade(instrument: str, exit_day: date, reason: str) -> TradeRow:
    one = Decimal(1)
    return TradeRow(
        entry_date=date(2024, 1, 1),
        exit_date=exit_day,
        playbook="default",
        instrument_id=instrument,
        qty=1,
        entry_price=one,
        initial_stop=one,
        exit_reason=reason,
        realized_pnl=one,
        fees=one,
        r_multiple=one,
        mfe_r=one,
        mae_r=one,
        holding_days=1,
        strategy_version_id=_UUID,
        risk_policy_version_id=_UUID,
    )


def _watch(day: date, instrument: str) -> WatchRow:
    return WatchRow(day, "default", instrument, Decimal(1), Decimal(10), Decimal(9))


def _fill(day: date, instrument: str) -> FillRow:
    return FillRow(day, instrument, "entry", "buy", 1, Decimal(10), Decimal(0))


def _result(
    days: list[date],
    trades: tuple[TradeRow, ...] = (),
    watchlists: tuple[WatchRow, ...] = (),
    fills: tuple[FillRow, ...] = (),
) -> BacktestResult:
    return BacktestResult(
        trades=trades,
        equity=tuple((d, Decimal(100)) for d in days),
        watchlists=watchlists,
        signals=(),
        fills=fills,
        skips={},
        metrics={},
    )


_DAYS = [date(2024, 1, d) for d in range(1, 21)]


def test_truncation__delisted_later_only_the_longer_runs_exits_through_t() -> None:
    at = _DAYS[9]
    short = _result(_DAYS[:10], trades=(_trade("a", _DAYS[3], DELISTED),))
    long = _result(
        _DAYS,
        trades=(
            _trade("a", _DAYS[3], DELISTED),  # the shorter run knew it
            _trade("b", _DAYS[5], DELISTED),
            _trade("b", _DAYS[7], DELISTED),  # the first exit of b counts
            _trade("c", _DAYS[4], END_OF_PERIOD),  # not a delisting
            _trade("d", _DAYS[12], DELISTED),  # after T
        ),
    )
    assert truncation.delisted_later(short, long, at) == {"b": _DAYS[5]}


def test_truncation__compare_without_a_cut_reads_decisions_through_t() -> None:
    at = _DAYS[9]
    short = _result(_DAYS[:10], watchlists=(_watch(at, "a"),), fills=(_fill(_DAYS[8], "a"),))
    long = _result(
        _DAYS,
        watchlists=(_watch(at, "a"), _watch(_DAYS[10], "z")),
        fills=(_fill(_DAYS[8], "a"), _fill(_DAYS[10], "z")),
    )
    checks = truncation.compare(short, long, at)
    assert all(c["pass"] for c in checks.values())
    assert checks["delisting_known_later"]["uncompared_sessions"] == 0
    assert checks["delisting_known_later"]["warning"] is None
    long = _result(_DAYS, watchlists=(_watch(at, "a"), _watch(at, "b")))
    assert not truncation.compare(short, long, at)["watchlists"]["pass"]


def test_truncation__compare_after_a_cut_reads_its_decisions_not_its_fills() -> None:
    at, cut = _DAYS[9], _DAYS[6]
    delisting = (_trade("x", cut, DELISTED),)
    short = _result(_DAYS[:10], watchlists=(_watch(cut, "a"),), fills=(_fill(cut, "a"),))
    # the cut's fills and the later sessions' decisions differ: not compared
    long = _result(
        _DAYS,
        trades=delisting,
        watchlists=(_watch(cut, "a"), _watch(_DAYS[7], "b")),
        fills=(_fill(cut, "b"),),
    )
    checks = truncation.compare(short, long, at)
    assert all(c["pass"] for c in checks.values())
    later = checks["delisting_known_later"]
    assert later["diverged_after_delisting_hindsight_on"] == cut.isoformat()
    assert later["uncompared_sessions"] == 3
    assert later["warning"] is None
    assert checks["fills_on_t"]["not_compared"] == "diverged"
    # a decision of the cut itself is compared
    long = _result(_DAYS, trades=delisting, watchlists=(_watch(cut, "b"),))
    assert not truncation.compare(short, long, at)["watchlists"]["pass"]


def test_truncation__compare_warns_on_a_cut_more_than_five_sessions_before_t() -> None:
    at = _DAYS[9]
    long = _result(_DAYS, trades=(_trade("x", _DAYS[3], DELISTED),))
    later = truncation.compare(_result(_DAYS[:10]), long, at)["delisting_known_later"]
    assert later["uncompared_sessions"] == 6
    assert "6 sessions" in later["warning"]


def test_truncation__compare_leaves_out_the_cuts_delisting_sales_of_rotation_holdings() -> None:
    at, cut = _DAYS[9], _DAYS[6]
    delisting = (_trade("x", cut, DELISTED),)
    sale = {"date": cut, "name": "r", "side": "sell", "reason": DELISTED}
    order = {"date": cut, "name": "q", "side": "buy", "reason": "rebalance"}
    short = replace(_result(_DAYS[:10]), rebalances=(order,))
    long = replace(_result(_DAYS, trades=delisting), rebalances=(order, sale))
    assert truncation.compare(short, long, at)["rebalances"]["pass"]
    # another order of the cut is still compared
    long = replace(_result(_DAYS, trades=delisting), rebalances=(sale,))
    assert not truncation.compare(short, long, at)["rebalances"]["pass"]


class _Browser:
    """A browser process that exits on `terminate` only when `obeys`."""

    def __init__(self, *, obeys: bool, running: bool = True) -> None:
        self.obeys, self.running, self.calls = obeys, running, []

    def poll(self) -> int | None:
        return None if self.running else 0

    def terminate(self) -> None:
        self.calls.append("terminate")
        self.running = not self.obeys

    def kill(self) -> None:
        self.calls.append("kill")
        self.running = False

    def wait(self, timeout: float) -> int:
        self.calls.append("wait")
        if self.running:
            raise fyers_login.subprocess.TimeoutExpired("browser", timeout)
        return 0


def test_fyers_login__close_browser_waits_and_kills_a_browser_that_stays() -> None:
    obeys = _Browser(obeys=True)
    fyers_login.close_browser(obeys, wait=0)
    assert obeys.calls == ["terminate", "wait"]
    stays = _Browser(obeys=False)
    fyers_login.close_browser(stays, wait=0)
    assert stays.calls == ["terminate", "wait", "kill", "wait"]
    gone = _Browser(obeys=True, running=False)
    fyers_login.close_browser(gone, wait=0)
    assert gone.calls == []


def test_fyers_login__remove_profile_retries_a_directory_still_being_written(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "fyers-login-x"
    profile.mkdir()
    failures = [OSError(39, "Directory not empty")] * 2
    pauses: list[float] = []

    def rmtree(path: str) -> None:
        if failures:
            raise failures.pop()
        fyers_login.shutil.rmtree(path)

    assert fyers_login.remove_profile(
        str(profile), tries=5, pause=1.0, rmtree=rmtree, sleep=pauses.append
    )
    assert pauses == [1.0, 1.0]
    assert not profile.exists()


def test_fyers_login__remove_profile_never_raises(tmp_path: Path) -> None:
    profile = tmp_path / "fyers-login-y"
    profile.mkdir()

    def stuck(_path: str) -> None:
        raise OSError(39, "Directory not empty")

    # every try fails; the last, error-ignoring pass still removes what it can
    assert fyers_login.remove_profile(
        str(profile), tries=3, pause=0, rmtree=stuck, sleep=lambda _s: None
    )
    assert fyers_login.remove_profile(
        str(tmp_path / "never-made"), rmtree=fyers_login.shutil.rmtree
    )


open_0920 = _script("research/open_to_0920.py")


def test_open_to_0920__medians_per_side_and_never_favourable() -> None:
    found = open_0920.adverse_medians([0.004, 0.001, -0.002, 0.003, 0.0])
    assert found["count"] == 5
    assert found["buy_side_median"] == 0.001
    assert found["sell_side_median"] == -0.001
    assert found["suggested"] == {"buy": 0.001, "sell": 0.0}  # a sale's median helps it: 0
    assert open_0920.adverse_medians([]) == {"count": 0}


def test_open_to_0920__rebalance_sessions_are_each_months_first() -> None:
    days = [date(2024, 1, 30), date(2024, 1, 31), date(2024, 2, 1), date(2024, 2, 2)]
    assert open_0920.rebalance_sessions(days) == {date(2024, 1, 30), date(2024, 2, 1)}


def test_open_to_0920__liquidity_reads_the_sessions_before() -> None:
    liquid = open_0920.Liquidity(Decimal(100), Decimal(50))
    for _ in range(19):
        liquid.feed([("a", Decimal(60), Decimal(150)), ("b", Decimal(40), Decimal(500))])
    assert not liquid.liquid("a")  # 19 sessions: too few
    liquid.feed([("a", Decimal(60), Decimal(150)), ("b", Decimal(40), Decimal(500))])
    assert liquid.liquid("a")
    assert not liquid.liquid("b")  # under the price floor
    assert not liquid.liquid("z")


def test_open_to_0920__moves_read_fitting_sessions_only() -> None:
    from app.hat.data.bars import Bar
    from app.hat.time import IST

    day = date(2024, 2, 1)
    first = datetime.combine(day, datetime.min.time().replace(hour=9, minute=15), tzinfo=IST)
    fits = (Bar("a", first, Decimal(100), Decimal(103), Decimal(99), Decimal(102), 10),)
    off = (Bar("b", first, Decimal(150), Decimal(151), Decimal(149), Decimal(150), 10),)
    opens = {"a": Decimal(100), "b": Decimal(100)}
    from app.hat.backtest.intraday import Admission

    refused_0920 = first + timedelta(minutes=5)  # a bucket the import refused, not stored
    admission = Admission(refused={("g", day): frozenset({refused_0920})}, gaps={})
    gapped = (Bar("g", first, Decimal(100), Decimal(101), Decimal(99), Decimal(100), 10),)

    def admits(name: str, bars: object, open_: Decimal) -> bool:
        return admission.admits(name, day, bars, open_, None)  # type: ignore[arg-type]

    opens = {**opens, "g": Decimal(100)}
    sessions = {"a": fits, "b": off, "g": gapped}
    # g's stored bars hold a bucket the import refused, so it is not quiet there: not read
    assert open_0920.moves_of(day, sessions, opens, ["a", "b", "c", "g"], admits) == [
        pytest.approx(0.02)
    ]


def test_open_to_0920__turnover_falls_back_to_close_times_volume() -> None:
    assert open_0920.turnover(Decimal(500), Decimal(10), 7) == Decimal(500)
    assert open_0920.turnover(None, Decimal(10), 7) == Decimal(70)
    assert open_0920.turnover(None, None, 7) is None
    assert open_0920.SERIES_IN == {"EQ", "BE"}


def test_open_to_0920__an_empty_sample_is_refused() -> None:
    assert open_0920.sample_refusal({"all_member_days": {"count": 0}}) == "empty_sample"
    assert open_0920.sample_refusal({}) == "empty_sample"
    assert open_0920.sample_refusal({"all_member_days": {"count": 12}}) is None


def test_open_to_0920__members_are_read_by_chain() -> None:
    lineage = {"old": "chain"}  # an older ISIN's id carried by the day's raw bars
    assert open_0920.member_ids(["old", "chain", "x", "y"], {"chain", "y"}, lineage) == {
        "old",
        "chain",
        "y",
    }


# ---------------------------------------------------------------------------------------------
# s1_setups_audit (S1 Phase 3)


def _path(*legs: tuple[int, float]) -> list[float]:
    """Closes along straight legs: each (bars, close at its end) from the last close."""
    closes = [legs[0][1]]
    for bars, to in legs[1:]:
        start = closes[-1]
        closes += [start + (to - start) * (k + 1) / bars for k in range(bars)]
    return closes


def _series(closes: list[float], spread: float = 0.005):  # noqa: ANN202 - the script's Series
    days = [date(2020, 1, 1) + timedelta(days=n) for n in range(len(closes))]
    high = [c * (1 + spread) for c in closes]
    low = [c * (1 - spread) for c in closes]
    return setups_audit.Series(days, high, low, closes, [1000.0] * len(closes))


def test_setups_audit__a_cheat_marks_h0_c_and_the_pause() -> None:
    # up to H0 at bar 60, down 30% to C at bar 80, back to 85 by bar 100, flat 6 bars
    series = _series(_path((1, 50.0), (60, 100.0), (20, 70.0), (20, 85.0), (6, 85.5)))
    params = setups_audit.BookCheatParams()
    found = setups_audit.markers("book_cheat", series, params)
    assert (found.h0, found.c) == (60, 80)
    end = len(series.dates) - 1
    assert found.tight is not None
    assert found.tight.end == end
    assert found.tight.depth_pct <= Decimal(6)
    assert found.pivot == found.tight.high
    assert found.stop(Decimal("0.5")) == found.tight.low * Decimal("0.995")
    assert found.dryup == (Decimal(1), Decimal(1))  # flat volume: every vr is 1
    assert found.contractions[0].start == 60
    assert found.note == ""


def test_setups_audit__a_failed_handle_still_marks_the_area_it_judged() -> None:
    # the lip is the last bar: a handle of one session
    series = _series(_path((1, 50.0), (60, 100.0), (20, 70.0), (25, 95.0)))
    found = setups_audit.markers("book_cup_handle", series, setups_audit.BookCupHandleParams())
    assert (found.h0, found.c) == (60, 80)
    assert found.note == "handle: handle_too_short"
    end = len(series.dates) - 1
    assert found.tight is not None
    assert (found.tight.start, found.tight.end) == (end, end)


def test_setups_audit__a_missing_piece_is_noted_not_marked() -> None:
    flat = _series([100.0] * 120)
    found = setups_audit.markers("book_power_play", flat, setups_audit.BookPowerPlayParams())
    assert found.note.startswith("no thrust")
    assert found.tight is None
    assert found.pivot is None
    assert found.stop(Decimal("0.5")) is None
    rising = _series(_path((1, 50.0), (60, 100.0)))
    found = setups_audit.markers("book_primary_base", rising, setups_audit.BookPrimaryBaseParams())
    assert found.note == "no base"  # the highest high is the last bar
    with pytest.raises(ValueError, match="unknown detector"):
        setups_audit.markers("book_vcp", flat, None)


def test_setups_audit__a_primary_base_starts_at_its_highest_high() -> None:
    series = _series(_path((1, 50.0), (40, 100.0), (10, 85.0), (10, 98.0), (8, 92.0), (5, 97.0)))
    found = setups_audit.markers("book_primary_base", series, setups_audit.BookPrimaryBaseParams())
    assert found.h0 == 40
    assert found.contractions[0].start == 40
    assert found.tight == found.contractions[-1]


def test_setups_audit__the_full_day_ratio_and_adv50() -> None:
    assert setups_audit.full_day_ratio([100.0] * 50 + [150.0]) == pytest.approx(1.5)
    assert setups_audit.full_day_ratio([100.0] * 50) is None  # not 51 volumes
    assert setups_audit.full_day_ratio([0.0] * 50 + [10.0]) is None
    assert setups_audit.full_day_ratio([float("nan")] + [100.0] * 50) is None
    means = setups_audit.adv([10.0] * 50 + [20.0, 30.0], days=50)
    assert means[:50] == [None] * 50
    assert means[50] == pytest.approx(10.0)
    assert means[51] == pytest.approx(10.2)
    assert setups_audit.adv([1.0, None, 1.0, 1.0], days=2) == [None, None, None, None]


def test_setups_audit__the_draw_is_seeded_and_one_per_key() -> None:
    items = [("a", 1), ("a", 2), ("b", 3), ("c", 4), ("d", 5)]
    first = setups_audit.draw(items, 3, 7, key=lambda x: x[0])
    assert first == setups_audit.draw(items, 3, 7, key=lambda x: x[0])
    assert len(first) == 3
    assert len({k for k, _ in first}) == 3
    assert first == sorted(first, key=items.index)  # in the items' order
    assert setups_audit.draw(items[:2], 15, 7) == items[:2]  # all of a small pool
    assert setups_audit.draw(items, 9, 7, key=lambda x: x[0]) == [
        i for i in items if i in setups_audit.draw(items, 9, 7, key=lambda x: x[0])
    ]
    assert len(setups_audit.draw(items, 9, 7, key=lambda x: x[0])) == 4  # 4 keys


def test_setups_audit__a_near_miss_names_its_threshold() -> None:
    cheat = setups_audit.BookCheatParams()
    assert setups_audit.threshold_of("no_pause", cheat) == "a 5-15 session range <= 6.0%"
    assert setups_audit.threshold_of("pause_in_upper_third", cheat) == (
        "pause high position <= 0.67"
    )
    play = setups_audit.BookPowerPlayParams()
    assert setups_audit.threshold_of("final_depth_too_deep", play) == ("final contraction <= 8.0%")
    assert setups_audit.threshold_of("no_volume_dryup", play) == "mean vr < 0.7"
    assert setups_audit.threshold_of("something_else", play) == "something_else"


def test_setups_audit__later_bars_join_on_the_nights_scale() -> None:
    night = _series([100.0, 110.0])
    later = _series([50.0, 55.0, 60.0])  # halved by a later split; one more bar
    joined = setups_audit.stitch(night, later)
    assert joined.dates == night.dates + later.dates[2:]
    assert joined.close == pytest.approx([100.0, 110.0, 120.0])
    assert joined.volume == [1000.0] * 3
    assert setups_audit.head(joined, 2) == night
    moved = replace(later, dates=[d + timedelta(days=9) for d in later.dates])
    assert setups_audit.stitch(night, moved) == night  # no shared night: nothing joined
