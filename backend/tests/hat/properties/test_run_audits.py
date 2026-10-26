"""The data audit's and the run audit's checks (docs/backtests/process.md), each on small
hand-built inputs, and the review request. No market load, no run of the loop."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.hat.backtest import compare, review, run_audit
from app.hat.backtest.runs import parse_variants
from app.hat.backtest.spec import INITIAL_EQUITY, RunConventions
from app.hat.data import audit

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
    check = audit.check_missing_sessions(DAYS, spans, bars, {"a": "AAA"}, DAYS[0], DAYS[-1])
    worst = {r["instrument_id"]: r for r in check["summary"]["worst"]}
    assert worst["a"]["missing"] == 2
    assert worst["a"]["longest_gap"] == 2
    assert worst["a"]["symbol"] == "AAA"
    assert "b" not in worst
    assert check["summary"]["missing"] == 2
    assert check["status"] == audit.FAIL  # 2 of 12 member sessions is above the universe's 2%
    assert _codes(check) == {"member_sessions_missing": 1, "universe_sessions_missing": 1}


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
    [found] = audit.check_features([("a", DAYS[0], stored, later), ("b", DAYS[1], None, same)])
    assert found["count"] == 2
    assert found["items"][0]["features"] == ["sma50"]
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


def test_bars_5m_grid_and_fit() -> None:
    at = datetime(2024, 1, 1, 3, 45, tzinfo=UTC)  # 09:15 IST
    good = audit.Day5m(
        "a",
        DAYS[0],
        [at, at + timedelta(minutes=5)],
        [(100.0, 101.0, 99.0, 100.5), (100.5, 102.0, 100.0, 101.0)],
        (100.0, 102.0, 99.0, 101.5),
    )
    assert audit.check_bars_5m([good], 0.005, used=False)["status"] == audit.PASS
    late = at + timedelta(hours=6, minutes=20)  # 15:35 IST, after the close
    shifted = audit.Day5m("a", DAYS[0], [late], good.bars[:1], good.daily)
    off = audit.Day5m("a", DAYS[0], good.starts, good.bars, (100.0, 110.0, 99.0, 101.5))
    check = audit.check_bars_5m([shifted, off], 0.005, used=True)
    assert _codes(check) == {"bar_5m_off_ist_grid": 1, "bar_5m_off_daily": 2}
    assert check["status"] == audit.FAIL
    assert audit.check_bars_5m([off], 0.005, used=False)["status"] == audit.WARN


def test_audit_doc_digest_and_cache(tmp_path: Path) -> None:
    checks = [audit.check_unique_keys([], [], []), audit._check("x", {}, [])]
    doc = audit.audit_doc(checks, {"data_snapshot_id": "abc"})
    assert doc["status"] == audit.PASS
    assert doc["digest"] == audit.audit_doc(checks, {"data_snapshot_id": "abc"})["digest"]
    assert doc["digest"] != audit.audit_doc(checks, {"data_snapshot_id": "abd"})["digest"]
    key = audit.cache_key("abc", DAYS[0], DAYS[1], used_5m=False)
    assert key != audit.cache_key("abc", DAYS[0], DAYS[2], used_5m=False)
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
    "date,equity,position_value\n2024-01-02,1000000,10000\n2024-01-05,1001000,6000\n"
    "2024-01-08,1000980,0\n"
)
REFUSALS = "date,symbol,reason\n2024-01-03,CCC,max_positions\n2024-01-04,DDD,max_positions\n"


def _audit(run: dict, trades: str = TRADES, equity: str = EQUITY, bars: dict = BARS) -> dict:
    doc = run_audit.audit_run(
        run, {"trades": trades, "equity": equity, "refusals": REFUSALS}, bars, D(20)
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


def test_equity_jumps_need_positions() -> None:
    jumpy = EQUITY + "2024-01-09,1100980,0\n2024-01-10,1200980,100000\n"
    checks = _audit(_run(), equity=jumpy)
    # 01-09: +10% with nothing held either side; 01-10: +9% with 100,000 held (beyond 20% of it)
    assert _codes(checks["equity_jumps"]) == {
        "equity_jump_without_positions": 1,
        "equity_jump_beyond_band": 1,
    }


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
                    "base": "base",
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


def test_compare_row() -> None:
    run = _run()
    run["identity"].update(run_id="019a2b3c-4d5e", variant="v1", change="a|b", status="complete")
    row = compare.row_of(run, {"status": "warn"})
    assert row[:4] == ["019a2b3c", "v1", "a/b", "complete"]
    assert row[-1] == "warn/pass"
    assert len(row) == len(compare.COLUMNS) + 2
    assert compare.table([row]).count("\n") == 3
