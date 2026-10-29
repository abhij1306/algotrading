"""The ledger reconciliation (`reconcile`) and the run audit's `ledger` check on one hand-worked
round trip: no market load, no run of the loop."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal

from app.hat.backtest import run_audit
from app.hat.backtest.reconcile import Held, SessionBook, reconcile
from app.hat.backtest.report import FillRow, LegRow, TradeRow

D = Decimal
D1, D2, D3 = date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)
START = D(100_000)

# buy 10 at 100 (fees 5); a Rs 2 dividend on the 10 shares; sell 10 at 110 (fees 6)
FILLS = (
    FillRow(D1, "abc", "entry", "buy", 10, D(100), D(5)),
    FillRow(D3, "abc", "exit", "sell", 10, D(110), D(6)),
)
# cash 100000 - 1000 - 5 = 98995; + 20 = 99015; + 1100 - 6 = 100109
BOOKS = (
    SessionBook(D1, D(100_015), D(98_995), D(1020), (Held("abc", 10, D(102)),)),
    SessionBook(D2, D(100_065), D(99_015), D(1050), (Held("abc", 10, D(105)),), dividends=D(20)),
    SessionBook(D3, D(100_109), D(100_109), D(0)),
)
TRADE = TradeRow(
    entry_date=D1,
    exit_date=D3,
    playbook="vcp",
    instrument_id="abc",
    qty=10,
    entry_price=D(100),
    initial_stop=D(95),
    exit_reason="target",
    realized_pnl=D(109),  # 1100 - 1000 - 11 + 20
    fees=D(11),
    r_multiple=D("2.18"),
    mfe_r=D(2),
    mae_r=D(0),
    holding_days=2,
    strategy_version_id=uuid.UUID(int=1),
    risk_policy_version_id=uuid.UUID(int=2),
    dividends=D(20),
    legs=(LegRow(D3, 10, D(110), D(6), "target"),),
)


def _codes(report: dict) -> set[str]:
    return {f["code"] for f in report["findings"]}


def test_a_round_trip_reconciles() -> None:
    report = reconcile(START, BOOKS, FILLS, (TRADE,))
    assert report["status"] == "pass", report
    assert report["dividends"] == "20"
    assert D(report["worst_cash_diff"]) == 0


def test_cash_off_by_more_than_a_rupee_fails() -> None:
    books = (BOOKS[0], replace(BOOKS[1], cash=D(99_013), equity=D(100_063)), BOOKS[2])
    assert _codes(reconcile(START, books, FILLS, (TRADE,))) == {"cash_walk"}
    within = (BOOKS[0], replace(BOOKS[1], cash=D("99015.5"), equity=D("100065.5")), BOOKS[2])
    assert reconcile(START, within, FILLS, (TRADE,))["status"] == "pass"


def test_holdings_marks_and_identity() -> None:
    lost = (BOOKS[0], replace(BOOKS[1], held=()), BOOKS[2])
    # the position the fills hold is missing, so its value is too
    assert _codes(reconcile(START, lost, FILLS, (TRADE,))) == {"holdings", "position_value"}
    unbalanced = (BOOKS[0], replace(BOOKS[1], equity=D(100_100)), BOOKS[2])
    assert _codes(reconcile(START, unbalanced, FILLS, (TRADE,))) == {"equity_identity"}
    unsold = reconcile(START, BOOKS, FILLS[:1], (TRADE,))
    assert {"holdings", "cash_walk", "instrument_pnl"} <= _codes(unsold)


def test_trade_pnl_and_dividends() -> None:
    wrong = replace(TRADE, realized_pnl=D(120))
    assert _codes(reconcile(START, BOOKS, FILLS, (wrong,))) == {"trade_pnl", "instrument_pnl"}
    unpaid = replace(TRADE, dividends=D(0), realized_pnl=D(89))
    assert _codes(reconcile(START, BOOKS, FILLS, (unpaid,))) == {"dividends"}
    stray = (*FILLS, FillRow(date(2024, 1, 4), "abc", "exit", "sell", 1, D(1), D(0)))
    assert "fill_outside_sessions" in _codes(reconcile(START, BOOKS, stray, (TRADE,)))


def test_the_run_audit_ledger_check() -> None:
    run = {"metrics": {"final_equity": 100_109.0}}
    rows = [
        {"date": "2024-01-01", "equity": "100015", "cash": "98995", "position_value": "1020"},
        {"date": "2024-01-03", "equity": "100109", "cash": "100109", "position_value": "0"},
    ]
    passed = {"status": "pass", "findings": []}
    assert run_audit.check_ledger(run, rows, passed)["status"] == "pass"
    failed = {"status": "fail", "findings": [{"code": "cash_walk"}]}
    assert _codes(run_audit.check_ledger(run, rows, failed)) == {"ledger_mismatch"}
    assert _codes(run_audit.check_ledger(run, rows, None)) == {"no_ledger"}
    off = [{**rows[0], "cash": "98000"}, {**rows[1], "position_value": "5"}]
    assert _codes(run_audit.check_ledger({"metrics": {"final_equity": 1.0}}, off, passed)) == {
        "equity_not_cash_plus_positions",
        "final_equity_disagrees",
        "held_after_period_end",
    }


def test_a_trade_bought_in_several_fills_is_checked_by_its_instrument() -> None:
    # a rotation's top-up: 5 at 100 then 5 at 102 (fees 5 in all), sold 10 at 110 (fees 6); the
    # trade's entry price is its first buy's, so only the instrument's P&L recomputes it
    fills = (
        FillRow(D1, "abc", "entry", "buy", 5, D(100), D(2)),
        FillRow(D2, "abc", "add", "buy", 5, D(102), D(3)),
        FillRow(D3, "abc", "exit", "sell", 10, D(110), D(6)),
    )
    books = (
        SessionBook(D1, D(100_008), D(99_498), D(510), (Held("abc", 5, D(102)),)),
        SessionBook(D2, D(99_995), D(98_985), D(1010), (Held("abc", 10, D(101)),)),
        SessionBook(D3, D(100_079), D(100_079), D(0)),
    )
    trade = replace(TRADE, realized_pnl=D(79), fees=D(11), dividends=D(0))  # 1100 - 1010 - 11
    assert reconcile(START, books, fills, (trade,))["status"] == "pass"
    wrong = replace(trade, realized_pnl=D(90))
    assert _codes(reconcile(START, books, fills, (wrong,))) == {"instrument_pnl"}
