"""The data audit's and the run audit's checks (docs/backtests/process.md), each on small
hand-built inputs, and the review request. No market load, no run of the loop."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.hat.backtest import compare, review, run_audit
from app.hat.backtest.runs import parse_variants
from app.hat.backtest.spec import INITIAL_EQUITY, RunConventions
from app.hat.data import audit
from app.hat.data.store import ActionFactor, Adjustment

D = Decimal
DAYS = [date(2024, 1, 1) + timedelta(days=n) for n in range(10)]  # ten "sessions"


def _codes(check: dict) -> dict[str, int]:
    return {f["code"]: f["count"] for f in check["findings"]}


# --- the data audit ------------------------------------------------------------------------------


def test_calendar_against_the_store() -> None:
    trading = DAYS[:5] + DAYS[6:]
    ok = audit.check_calendar(trading, trading, DAYS[0], DAYS[-1])
    assert ok["status"] == audit.PASS
    shifted = audit.check_calendar(DAYS[:5] + DAYS[6:9], trading, DAYS[0], DAYS[-1] + timedelta(5))
    # DAYS[9] is a session after the store's last day: the data ends early, no gap
    assert _codes(shifted) == {"data_ends_before_period": 1}
    holiday = audit.check_calendar(DAYS, trading, DAYS[0], DAYS[-1])
    assert _codes(holiday) == {"store_day_not_session": 1}
    gap = audit.check_calendar(DAYS[:3] + DAYS[4:5] + DAYS[6:], trading, DAYS[0], DAYS[-1])
    assert _codes(gap) == {"session_missing_from_store": 1}
    assert gap["status"] == audit.FAIL


def test_missing_sessions_while_member_and_listed() -> None:
    spans = {"a": [(DAYS[0], None)], "b": [(DAYS[2], DAYS[6])]}
    bars = {
        "a": [d for d in DAYS if d not in (DAYS[4], DAYS[5])],  # a two-session gap
        "b": DAYS[3:5],  # listed from DAYS[3]: DAYS[2] is before its first bar, DAYS[5] missing
    }
    isins = {"a": {"INE000A01011"}, "b": {"INE000B01011"}}
    traded = {d: frozenset({"INE000A01011", "INE000B01011"}) for d in DAYS}
    check = audit.check_missing_sessions(
        DAYS, spans, bars, {"a": "AAA"}, DAYS[0], DAYS[-1], isins=isins, traded=traded
    )
    worst = {r["instrument_id"]: r for r in check["summary"]["worst"]}
    assert worst["a"]["missing"] == 2
    assert worst["a"]["longest_gap"] == 2
    assert worst["a"]["symbol"] == "AAA"
    assert "b" not in worst
    assert check["summary"]["missing"] == 2
    assert check["status"] == audit.FAIL  # 2 of 12 member sessions is above the universe's 2%
    assert _codes(check) == {"member_sessions_missing": 1, "universe_sessions_missing": 1}
    # NSE printed no row for "a" on its gap days: no-trade days, reported and never missing
    idle = {d: frozenset({"INE000B01011"}) for d in DAYS}
    quiet = audit.check_missing_sessions(
        DAYS, spans, bars, {"a": "AAA"}, DAYS[0], DAYS[-1], isins=isins, traded=idle
    )
    assert quiet["status"] == audit.PASS
    assert quiet["summary"]["missing"] == 0
    assert quiet["summary"]["member_sessions"] == 10  # 12 member sessions less 2 no-trade days
    assert quiet["summary"]["no_trade"] == 2
    assert quiet["summary"]["no_trade_by_member"] == {"a": 2}
    assert quiet["summary"]["no_trade_worst"][0]["longest_no_trade"] == 2


def test_split_gaps_by_what_nse_printed() -> None:
    chain = {"INE111A01011", "INE111A01029"}  # an old and a new ISIN of one chain
    traded = {
        DAYS[0]: frozenset({"INE111A01011"}),  # NSE has the old ISIN's bar: missing
        DAYS[1]: frozenset({"INE111A01029", "INE999Z01019"}),  # the new ISIN's: missing
        DAYS[2]: frozenset({"INE999Z01019"}),  # another stock only: no trade
        DAYS[3]: frozenset(),  # nothing printed for the chain: no trade
    }  # DAYS[4] has no raw file
    gaps = audit.split_gaps(DAYS[:5], chain, traded)
    assert gaps.missing == [DAYS[0], DAYS[1], DAYS[4]]
    assert gaps.no_trade == [DAYS[2], DAYS[3]]
    assert gaps.raw_absent == [DAYS[4]]
    assert audit.split_gaps([], chain, traded) == audit.Gaps([], [], [])


def test_survivorship_needs_leavers_with_bars() -> None:
    year = [date(2020, 1, 1) + timedelta(days=n) for n in range(0, 800, 7)]
    end = year[-1]
    spans = {"stay": [(date(2016, 1, 1), None)], "left": [(date(2016, 1, 1), year[40])]}
    bars = {"stay": year, "left": year[:41]}
    ok = audit.check_survivorship(year, spans, bars, {}, {}, year[0], end)
    assert ok["status"] == audit.PASS
    assert ok["summary"]["leavers"] == 1
    assert ok["summary"]["samples"][0]["later_left"] == 1
    no_bars = audit.check_survivorship(year, spans, {"stay": year}, {}, {}, year[0], end)
    assert _codes(no_bars) == {"leaver_without_bars": 1}
    todays = audit.check_survivorship(year, {"stay": spans["stay"]}, bars, {}, {}, year[0], end)
    assert set(_codes(todays)) == {"no_leavers"}
    delisted = audit.check_survivorship(
        year, {"stay": spans["stay"]}, bars, {"stay": year[50]}, {}, year[0], end
    )
    assert delisted["summary"]["leavers"] == 1
    assert delisted["status"] == audit.PASS


def test_jumps_at_ex_dates_and_action_shaped() -> None:
    days = DAYS[:4]
    # a 2:1 split on DAYS[2]: the raw close halves, the factor halves with it: no jump
    split = (days, [50.0, 51.0, 52.0, 52.5], [100.0, 102.0, 52.0, 52.5])
    # a split with no factor: the adjusted close halves too
    missed = (days, [100.0, 102.0, 51.5, 52.0], [100.0, 102.0, 51.5, 52.0])
    # a factor that over-adjusts on its ex-date (adjusted +44%, raw -75%)
    over = (days, [100.0, 101.0, 145.0, 146.16], [100.0, 101.0, 25.0, 25.2])
    real = (days, [100.0, 130.0, 131.0, 132.0], [100.0, 130.0, 131.0, 132.0])  # a 30% day
    check = audit.check_corporate_actions(
        {"split": split, "missed": missed, "over": over, "real": real}, DAYS, (), {}
    )
    assert check["summary"]["ex_dates"] == 2
    assert _codes(check) == {"ex_date_jump": 1, "action_shaped_jump": 1}
    assert check["summary"]["jumps_elsewhere"] == 2  # the missed split and the real move
    skipped = audit.check_corporate_actions({"missed": missed}, DAYS, {("missed", DAYS[2])}, {})
    assert skipped["status"] == audit.PASS
    assert skipped["summary"]["jumps_already_skipped"] == 1
    assert audit.action_shaped(-50.4)
    assert audit.action_shaped(-66.0)
    assert not audit.action_shaped(-33.0)  # a crash, not a 1:2 bonus
    assert not audit.action_shaped(-42.6)
    assert not audit.action_shaped(30.0)


def test_feature_and_breadth_samples() -> None:
    stored = dict.fromkeys(audit.PER_INSTRUMENT, 1.0)
    same = {**stored, "sma50": 1.0 + 1e-13}
    later = {**stored, "sma50": 1.01}  # a value that read a later bar
    assert audit.check_features([("a", DAYS[0], stored, same)]) == []
    found, missing = audit.check_features(
        [("a", DAYS[0], stored, later), ("b", DAYS[1], stored, None), ("c", DAYS[1], None, same)]
    )
    assert (found["code"], found["count"]) == ("feature_not_point_in_time", 2)
    assert [i["features"] for i in found["items"]] == [["sma50"], ["row"]]
    assert (missing["code"], missing["count"]) == ("feature_row_missing", 1)
    isin = "INE001A01036"
    pct, counted = audit.recount_breadth(
        [(isin, 11.0, 10.0), (isin, 9.0, 10.0), (isin, 9.0, None), ("INF001A01036", 20.0, 1.0)]
    )
    assert (pct, counted) == (50.0, 2)
    assert audit.check_breadth([(DAYS[0], 50.0, 50.0, 2)]) == []
    assert audit.check_breadth([(DAYS[0], 50.0, 49.0, 200)])  # more than one stock off
    assert audit.check_breadth([(DAYS[0], 50.0, 49.8, 200)]) == []  # within one stock


def test_gates_read_the_session_before() -> None:
    found = audit.check_gate_inputs(DAYS, DAYS[:5], DAYS, DAYS[3], DAYS[-1])
    # D's gate reads D-1: DAYS[6..9] read DAYS[5..8], which have no index close
    assert [(f["code"], f["count"]) for f in found] == [("no_index_close_before", 4)]
    assert audit.check_gate_inputs(DAYS, DAYS, DAYS, DAYS[1], DAYS[-1]) == []


def test_unique_keys() -> None:
    assert audit.check_unique_keys([], [("a", DAYS[0])], [("X", DAYS[0])])["status"] == audit.PASS
    check = audit.check_unique_keys(
        [{"instrument_id": "a", "date": "2024-01-01", "rows": 2, "isins": 2, "runs": 1}],
        [("a", DAYS[0]), ("a", DAYS[0])],
        [("X", DAYS[0]), ("X", DAYS[1])],
    )
    assert _codes(check) == {"duplicate_bar": 1, "duplicate_feature_row": 1}


def test_bars_5m_scan_names_the_adjusted_basis() -> None:
    def day(last: float, daily: float | None, later: float = 1.0, grid: int = 0) -> audit.Day5m:
        return audit.Day5m("a", DAYS[0], grid, last, daily, later, member=True)

    assert audit.basis_of(day(100.4, 100.0)) is None  # the close is a 30-minute average
    assert audit.basis_of(day(50.1, 100.0, later=0.5)) == "adjusted_basis"
    assert audit.basis_of(day(50.1, 100.0)) == "close_off"
    assert audit.basis_of(day(100.0, None)) == "no_daily"
    assert audit.basis_of(day(100.0, 100.0, grid=1)) == "off_grid"
    check = audit.check_bars_5m([day(50.1, 100.0, later=0.5), day(100.0, None)], 10, used=True)
    assert _codes(check) == {"bar_5m_adjusted_basis": 1, "bar_5m_no_daily": 1}
    assert check["status"] == audit.WARN  # a run decides such a day on the daily estimate
    assert audit.check_bars_5m([day(100.4, 100.0)], 10, used=True)["status"] == audit.PASS


def test_later_factors_multiply_the_ex_dates_after_a_day() -> None:
    adjustment = Adjustment(
        (ActionFactor("c", DAYS[2], Decimal("0.5")), ActionFactor("c", DAYS[5], Decimal("0.2"))),
        {},
    )
    factors = audit.later_factors(adjustment)
    assert audit._later(factors, "c", DAYS[0]) == pytest.approx(0.1)
    assert audit._later(factors, "c", DAYS[2]) == pytest.approx(0.2)  # the ex-day is adjusted
    assert audit._later(factors, "c", DAYS[5]) == 1.0
    assert audit._later(factors, "x", DAYS[0]) == 1.0


def test_industries_warn_on_unknown() -> None:
    spans = {"a": [(DAYS[0], None)], "b": [(DAYS[0], DAYS[5])]}
    history = {"a": [(DAYS[0], "Power")], "b": [(DAYS[3], "Realty")]}
    check = audit.check_industries(DAYS, spans, history, {})
    assert check["summary"]["member_days"] == len(DAYS) + 5
    assert check["summary"]["unknown_member_days"] == 3
    assert _codes(check) == {"industry_unknown": 1}
    full = {"a": history["a"], "b": [(DAYS[0], "Realty")]}
    assert audit.check_industries(DAYS, spans, full, {})["status"] == audit.PASS


def test_industry_cap_refuses_only_a_capped_run() -> None:
    """One shared audit with unknown member-days: a sweep's capped variant is refused, its
    uncapped sibling is not (CodeAnt #284)."""
    spans = {"a": [(DAYS[0], None)]}
    partial = audit.audit_doc(
        [audit.check_industries(DAYS, spans, {"a": [(DAYS[3], "Power")]}, {})], {}
    )
    assert audit.industry_cap_refused(partial, max_per_industry=2, max_open=10)
    assert not audit.industry_cap_refused(partial, max_per_industry=10, max_open=10)
    complete = audit.audit_doc(
        [audit.check_industries(DAYS, spans, {"a": [(DAYS[0], "Power")]}, {})], {}
    )
    assert not audit.industry_cap_refused(complete, max_per_industry=2, max_open=10)
    assert audit.industry_cap_refused({"checks": []}, max_per_industry=2, max_open=10)


def test_demergers_flag_a_thin_open_not_corrected() -> None:
    def demerger(factor: float, announced: date | None = DAYS[0]) -> audit.Demerger:
        return audit.Demerger("c", "S", DAYS[4], factor, 0.172, 0.248, announced)

    assert _codes(audit.check_demergers([demerger(0.172)], DAYS[0], DAYS[-1])) == {
        "demerger_thin_open": 1
    }
    corrected = audit.check_demergers([demerger(0.2483, None)], DAYS[0], DAYS[-1])
    assert corrected["status"] == audit.PASS
    assert corrected["summary"]["undated_member_demergers"] == 1
    assert (
        audit.check_demergers([demerger(0.2483, None)], DAYS[5], DAYS[-1])["summary"][
            "undated_member_demergers"
        ]
        == 0
    )


def test_audit_doc_digest_and_cache(tmp_path: Path) -> None:
    checks = [audit.check_unique_keys([], [], []), audit.check_result("x", {}, [])]
    doc = audit.audit_doc(checks, {"data_snapshot_id": "abc"})
    assert doc["status"] == audit.PASS
    assert doc["digest"] == audit.audit_doc(checks, {"data_snapshot_id": "abc"})["digest"]
    assert doc["digest"] != audit.audit_doc(checks, {"data_snapshot_id": "abd"})["digest"]
    key = audit.cache_key("abc", DAYS[0], DAYS[1], used_5m=False, golden_digest=None)
    assert key != audit.cache_key("abc", DAYS[0], DAYS[2], used_5m=False, golden_digest=None)
    assert key != audit.cache_key("abc", DAYS[0], DAYS[1], used_5m=False, golden_digest="d")
    calls = []

    def compute() -> dict:
        calls.append(1)
        return doc

    first, path = audit.cached(tmp_path, key, compute)
    again, _ = audit.cached(tmp_path, key, compute)
    assert first == again == doc
    assert len(calls) == 1
    assert path.is_file()


# --- the run audit -------------------------------------------------------------------------------

TRADE_HEAD = (
    "trade_id,leg,instrument_id,symbol,signal_date,entry_date,entry_price,risk_per_share,qty,"
    "exit_date,exit_price,gross_pnl,fees,net_pnl,r_net,adv_20d_cr,position_pct_adv\n"
)


def _run(**metrics: object) -> dict:
    return {
        "identity": {"run_id": "r", "base_run_id": None},
        "data": {"audit": {"digest": "d", "status": "pass"}},
        "config": {"start_capital": 1000000, "cost_model": "cnc"},
        "metrics": {
            "trades": 2,
            "final_equity": 1000980.0,
            "fees": 120.0,
            "expectancy_r": 0.98,
            "win_pct": 50.0,
            "cost_share_pct": 10.0,
            "top5_trades_pnl_share_pct": 100.0,
            "cagr_pct": 1.0,
            "cagr_ex_top5_trades_pct": -1.0,
            "dsr_n_trials": 3,
            "dsr_prob": 0.4,
            **metrics,
        },
        "refusals": {"max_positions": 2},
        "binding": {},
    }


# trade 1: 100 shares at 100, risk 5/share, two legs (+1000 gross, +600 gross) less 60 fees;
# trade 2: 50 shares at 200, risk 10/share, -500 gross less 60 fees
TRADES = (
    TRADE_HEAD
    + (
        "1,1,a,AAA,2024-01-01,2024-01-02,100,5,50,2024-01-05,120,1000,30,970,1.94\n"
        "1,2,a,AAA,2024-01-01,2024-01-02,100,5,50,2024-01-08,112,600,30,570,1.14,10,0.001\n"
    ).replace("1.94\n", "1.94,10,0.001\n")
    + ("2,1,b,BBB,2024-01-03,2024-01-04,200,10,50,2024-01-05,190,-500,60,-560,-1.12,1,0.1\n")
)
BARS = {
    ("a", DAYS[1]): (D(99), D(101)),
    ("a", DAYS[4]): (D(118), D(121)),
    ("a", DAYS[7]): (D(110), D(113)),
    ("b", DAYS[3]): (D(195), D(201)),
    ("b", DAYS[4]): (D(189), D(195)),
}
EQUITY = (
    "date,equity,position_value,cash\n2024-01-02,1000000,10000,990000\n"
    "2024-01-05,1001000,6000,995000\n2024-01-08,1000980,0,1000980\n"
)
LEDGER = {"status": "pass", "findings": []}
REFUSALS = "date,symbol,reason\n2024-01-03,CCC,max_positions\n2024-01-04,DDD,max_positions\n"


def _audit(run: dict, trades: str = TRADES, equity: str = EQUITY, bars: dict = BARS) -> dict:
    doc = run_audit.audit_run(
        run, {"trades": trades, "equity": equity, "refusals": REFUSALS}, bars, D(20), ledger=LEDGER
    )
    return {c["name"]: c for c in doc["checks"]} | {"_": doc}


def test_a_consistent_run_passes_every_check() -> None:
    checks = _audit(_run())
    statuses = {k: v["status"] for k, v in checks.items() if k != "_"}
    # the only warnings: five trades carry more than half the P&L, and the edge goes with them
    assert {k for k, v in statuses.items() if v != "pass"} == {"concentration"}
    assert _codes(checks["concentration"]) == {"top5_concentrated": 1, "edge_in_top5": 1}
    assert checks["fills"]["summary"]["fills_checked"] == 5


def test_run_audit_catches_what_does_not_add_up() -> None:
    bad = TRADES.replace("2024-01-08,112,600,30,570,1.14", "2024-01-08,130,600,30,571,1.14")
    bad = bad.replace("2,1,b,BBB,2024-01-03", "2,1,b,BBB,2024-01-05")  # signalled after entry
    checks = _audit(_run(trades=3, fees=0.0), trades=bad)
    assert _codes(checks["fills"])["fill_outside_bar"] == 1  # 130 above the day's high
    assert _codes(checks["sequence"]) == {"exit_before_entry": 1}
    pnl = checks["pnl"]["findings"]
    assert {f["code"] for f in pnl} == {
        "leg_net_not_gross_less_fees",
        "r_not_net_over_risk",
        "metrics_disagree_with_trades",
    }
    assert checks["_"]["status"] == run_audit.FAIL


def test_run_audit_refusals_trials_binding_and_data() -> None:
    run = _run(dsr_n_trials=None)
    run["refusals"] = {"max_positions": 1, "industry_cap": 1}
    run["identity"]["base_run_id"] = "base"
    run["binding"] = {"trade_list_hash": "h", "no_op": True}
    run["data"]["audit"]["status"] = "warn"
    checks = _audit(run)
    assert _codes(checks["refusals"]) == {"refusal_counts_disagree": 2}
    assert _codes(checks["trials"]) == {"no_trial_count": 1}
    assert _codes(checks["binding"]) == {"no_op": 1}
    assert _codes(checks["data"]) == {"data_audit_warn": 1}
    data_audit = {
        "checks": [{"name": "golden", "status": "pass"}, {"name": "calendar", "status": "warn"}]
    }
    named = run_audit.check_data(run, data_audit, "x.explained.md")["findings"][0]
    assert (named["checks"], named["explained"]) == (["calendar"], "x.explained.md")


def test_equity_jumps_need_positions() -> None:
    jumpy = EQUITY + "2024-01-09,1100980,0\n2024-01-10,1200980,100000\n"
    checks = _audit(_run(), equity=jumpy)
    # 01-09: +10% with nothing held either side; 01-10: +9% with 100,000 held (beyond 20% of it)
    assert _codes(checks["equity_jumps"]) == {
        "equity_jump_without_positions": 1,
        "equity_jump_beyond_band": 1,
    }


def test_a_same_day_round_trip_is_held() -> None:
    """A position opened and stopped out in one session shows in no close of `equity.csv`, but
    explains that session's move: held at its entry value (2598 x 676 here)."""
    trades = {
        "1": [
            {
                "entry_date": "2024-01-09",
                "entry_price": "676",
                "exit_date": "2024-01-09",
                "qty": "2598",
            }
        ]
    }
    assert run_audit.round_trips(trades) == {"2024-01-09": D(676) * 2598}
    lost = EQUITY + "2024-01-09,949980,0\n"  # -5.1% with nothing held at either close
    rows = run_audit._rows(lost)
    assert run_audit.check_equity_jumps(_run(), rows, trades)["findings"] == []
    unheld = run_audit.check_equity_jumps(_run(), rows, {})["findings"]
    assert [f["code"] for f in unheld] == ["equity_jump_without_positions"]


def test_liquidity_recomputed() -> None:
    checks = _audit(_run())
    # trade 2: 50 x 200 = 10,000 over 1 crore = 0.1% of ADV
    assert checks["liquidity"]["summary"]["max_position_pct_adv"] == 0.1
    wrong = TRADES.replace("-1.12,1,0.1", "-1.12,1,0.2")
    assert _codes(_audit(_run(), trades=wrong)["liquidity"]) == {"position_pct_adv_wrong": 1}


# --- variants and the review request -------------------------------------------------------------


def test_variants_set_the_fill_conventions_and_capital() -> None:
    sweep = parse_variants(
        {
            "sweep_id": "s",
            "variants": [
                {"label": "base"},
                {
                    "label": "v",
                    "entry_fill": "touch",
                    "ambiguous": "pessimistic",
                    "suspension_sessions": 3,
                    "regime_mult": "policy",
                    "start_capital": 2500000,
                },
            ],
        },
        RunConventions(),
    )
    base, v = (x.conventions for x in sweep.variants)
    assert base.start_capital == INITIAL_EQUITY
    assert (v.entry_fill_rule, v.ambiguous, v.suspension_sessions, v.regime_mult) == (
        "touch",
        "pessimistic",
        3,
        "policy",
    )
    assert v.start_capital == D(2500000)


def test_review_request_names_every_file(tmp_path: Path) -> None:
    run = _run()
    run["identity"].update(strategy="S1", run_id="r", base_run_id="b")
    text = review.request_text(
        run,
        {"status": "pass"},
        None,
        {
            "folder": tmp_path / "r",
            "standard": tmp_path / "std.md",
            "process": tmp_path / "process.md",
            "criteria": None,
            "base": tmp_path / "b",
            "validates": None,
        },
    )
    for part in ("audit.json", "data_audit.json", "std.md", "process.md", str(tmp_path / "b")):
        assert part in text
    assert "run audit: pass; data audit: absent" in text
    assert "verdict is `reject`" in text
    assert review.REPORT in text


def test_review_writes_the_criteria_table(tmp_path: Path) -> None:
    run_id = "01a17359-fe35-7207-abe4-fe26eae939bf"
    out = tmp_path / "backtests" / "runs" / run_id
    out.mkdir(parents=True)
    run = _run()
    run["identity"].update(strategy="S1", run_id=run_id)
    (out / "run.json").write_text(json.dumps(run), encoding="utf-8")
    with pytest.raises(ValueError, match="criteria_block_missing"):
        review.write_review(tmp_path, run_id, ("notes.md", "no block"))
    assert not (out / review.REQUEST).exists()
    notes = (
        "# S1\n```hat-criteria\nstrategy: S1\nuniverse: U\n"
        "in_sample: {from: 2024-01-01, to: 2024-01-31}\ntrial_budget: 30\n"
        "checks: {trades_min: 100}\n```\n"
    )
    text = review.write_review(tmp_path, run_id, ("notes.md", notes)).read_text()
    assert review.CRITERIA_MD in text
    assert "**fail**" in (out / review.CRITERIA_MD).read_text()  # 3 trades, and no plateau


def test_compare_row() -> None:
    run = _run()
    run["identity"].update(run_id="019a2b3c-4d5e", variant="v1", change="a|b", status="complete")
    row = compare.row_of(run, {"status": "warn"})
    assert row[:4] == ["019a2b3c", "v1", "a/b", "complete"]
    assert row[-1] == "warn/pass"
    assert len(row) == len(compare.COLUMNS) + 5
    assert compare.table([row]).count("\n") == 3
    assert compare.row_of(run, None, invested_max=81.5)[-4] == "81.5"


def test_max_invested() -> None:
    equity = "date,invested_pct\n2016-01-01,0.0\n2016-01-04,72.25\n2016-01-05,\n"
    assert compare.max_invested(equity) == 72.25
    assert compare.max_invested("date,invested_pct\n") is None
    assert compare.max_invested(None) is None


def test_check_lookahead_warns_on_a_decision_made_in_hindsight() -> None:
    """A read past the as-of clock a decision made warns `lookahead_hindsight` with its count; a
    descriptive column read in hindsight does not. Harm: a result resting on same-day volume or
    a registry delisting passing as point in time."""
    assert run_audit.check_lookahead({})["status"] == "pass"
    assert run_audit.check_lookahead({"breakout_vol_ratio": 9})["status"] == "pass"
    found = run_audit.check_lookahead({"ep_estimate_volume": 3, "breakout_vol_ratio": 9})
    assert found["status"] == "warn"
    assert found["findings"][0]["code"] == "lookahead_hindsight"
    assert found["findings"][0]["items"] == [{"ep_estimate_volume": 3}]
    assert found["summary"]["hindsight"] == {"breakout_vol_ratio": 9, "ep_estimate_volume": 3}
