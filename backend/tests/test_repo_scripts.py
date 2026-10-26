"""The pure seams of the repo's operator, data and agent scripts (scripts/): argument and path
handling, parsers and the merge check's verdict, on hand-built inputs. Nothing here reaches the
network, a broker, herdr, git or a database."""

import importlib.util
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

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
    events = n500.notice_events(parsed, overrides, date(2016, 1, 1))
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
    assert not n500.wanted_notice("Jan 05, 2010", "Replacements in indices")


# ---------------------------------------------------------------------------------------------
# set_secrets, fyers_login


def test_set_secrets__updated_env() -> None:
    text = "A=1\nexport KEY=old\n# note\nKEY=dup\n"
    assert secrets.updated_env(text, {"KEY": "new", "NEW": "v"}) == "A=1\nKEY=new\n# note\nNEW=v\n"


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
    threads = [_thread(True, "bot"), _thread(False, "bot", "owner"), _thread(False, "owner", "bot")]
    graphql = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": threads}}}}}
    assert merge.open_threads(graphql, "owner") == ["f.py (last: bot)"]


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
        ]
    )
    assert merge.secret_hits(diff) == [
        "app/x.py:AWS key",
        "deploy.env:env secret",
        "deploy.env:quoted secret",
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
