"""The ledger reconciliation (`reconcile`) and the run audit's `ledger` check on hand-built
ledgers over a hand-built market: no market load, no run of the loop. Each check is shown
catching a ledger with its bug."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from app.hat.backtest import run_audit
from app.hat.backtest.reconcile import Held, OverlayJournal, SessionBook, reconcile
from app.hat.backtest.report import FillRow, LegRow, TradeRow
from app.hat.costs import CostModel, charges, load_cost_schedule

D = Decimal
D1, D2, D3 = date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)
SCHEDULE = load_cost_schedule()  # the 2023-04-01 file is in force on these days
START = D(100_000)


@dataclass
class _Market:
    """A hand-built `reconcile.Market`: bars (low, high, close) by (instrument, day)."""

    bars: dict[tuple[str, date], tuple[Decimal, Decimal, Decimal]]
    dividends: dict[tuple[str, date], Decimal] = field(default_factory=dict)
    bps: Decimal = D(0)
    suspension_sessions: int = 5
    days: tuple[date, ...] = (D1, D2, D3)

    def model(self, day: date) -> CostModel:
        return SCHEDULE.on(day)

    def slippage_bps(self, instrument_id: str) -> Decimal:
        return self.bps

    def bar(self, instrument_id: str, day: date) -> tuple[Decimal, Decimal, Decimal] | None:
        return self.bars.get((instrument_id, day))

    def last_close(self, instrument_id: str, day: date) -> tuple[date, Decimal] | None:
        seen = [d for d in self.days if d <= day and (instrument_id, d) in self.bars]
        return (seen[-1], self.bars[(instrument_id, seen[-1])][2]) if seen else None

    def stale_sessions(self, instrument_id: str, seen: date, day: date) -> int:
        return self.days.index(day) - self.days.index(seen)

    def tick(self, instrument_id: str, day: date) -> Decimal:
        return D("0.05")

    def dividend(self, instrument_id: str, day: date) -> Decimal:
        return self.dividends.get((instrument_id, day), D(0))


BARS = {
    ("abc", D1): (D(98), D(104), D(102)),
    ("abc", D2): (D(101), D(106), D(105)),
    ("abc", D3): (D(108), D(112), D(110)),
}
MARKET = _Market(BARS, {("abc", D2): D(2)})

# buy 10 at 100 (charges 1.19); a Rs 2 dividend on the 10 shares; sell 10 at 110 (15.90 with DP)
FILLS = (
    FillRow(D1, "abc", "entry", "buy", 10, D(100), D("1.19")),
    FillRow(D3, "abc", "exit", "sell", 10, D(110), D("15.90")),
)
# cash 100000 - 1000 - 1.19 = 98998.81; + 20 = 99018.81; + 1100 - 15.90 = 100102.91
BOOKS = (
    SessionBook(D1, D("100018.81"), D("98998.81"), D(1020), (Held("abc", 10, D(102)),)),
    SessionBook(
        D2, D("100068.81"), D("99018.81"), D(1050), (Held("abc", 10, D(105)),), dividends=D(20)
    ),
    SessionBook(D3, D("100102.91"), D("100102.91"), D(0)),
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
    realized_pnl=D("102.91"),  # 1100 - 1000 - 17.09 + 20
    fees=D("17.09"),
    r_multiple=D("2.06"),
    mfe_r=D(2),
    mae_r=D(0),
    holding_days=2,
    strategy_version_id=uuid.UUID(int=1),
    risk_policy_version_id=uuid.UUID(int=2),
    dividends=D(20),
    legs=(LegRow(D3, 10, D(110), D("15.90"), "target"),),
)


def _codes(report: dict) -> set[str]:
    return {f["code"] for f in report["findings"]}


def _run(
    books: tuple[SessionBook, ...] = BOOKS,
    fills: tuple[FillRow, ...] = FILLS,
    trades: tuple[TradeRow, ...] = (TRADE,),
    market: _Market = MARKET,
    journal: OverlayJournal | None = None,
) -> dict:
    return reconcile(START, books, fills, trades, market, journal or OverlayJournal())


def test_a_round_trip_reconciles() -> None:
    report = _run()
    assert report["status"] == "pass", report
    assert report["dividends"] == "20.00"
    assert D(report["worst_cash_diff"]) == 0


def test_a_wrong_charge_is_caught() -> None:
    """Fees are recomputed from the schedule, never read back: a fill charged 5 where the
    schedule says 1.19 fails `charges`. Harm: a cost model bug invisible to the walk."""
    fills = (replace(FILLS[0], fees=D(5)), FILLS[1])
    assert "charges" in _codes(_run(fills=fills))


def test_a_charge_under_another_days_schedule_is_caught() -> None:
    """A 2024 fill charged at the 2016 file's rates (1.13, not 1.19) fails `charges`. Harm: the
    dated schedule ignored."""
    stale = charges(SCHEDULE.on(date(2016, 1, 4)), "buy", 10, D(100)).total
    assert stale != FILLS[0].fees
    assert "charges" in _codes(_run(fills=(replace(FILLS[0], fees=stale), FILLS[1])))


def test_the_dp_charge_is_once_per_instrument_per_day() -> None:
    """Two sales of one instrument on one day pay one DP charge: the first 15.32, the second
    0.57; a second DP debit fails `charges`."""
    split = (FILLS[0], replace(FILLS[1], qty=5, fees=D("15.32")))
    once = (*split, replace(FILLS[1], qty=5, fees=D("0.57")))
    twice = (*split, replace(FILLS[1], qty=5, fees=D("15.32")))
    assert "charges" not in _codes(_run(fills=once))
    assert "charges" in _codes(_run(fills=twice))


def test_a_fill_outside_its_bar_is_caught() -> None:
    """A buy prints between the bar's low and its high after slippage, a sale between the low
    after slippage and the high. Harm: a fill at a price that never traded."""
    above = (replace(FILLS[0], price=D("104.21")), FILLS[1])
    assert "fill_outside_bar" in _codes(_run(fills=above))
    # 20 bps lets a buy print up to 104 x 1.002 = 104.208, no further
    slipped = _Market(BARS, MARKET.dividends, bps=D(20))
    assert "fill_outside_bar" not in _codes(
        _run(fills=(replace(FILLS[0], price=D("104.2")),), market=slipped)
    )
    assert "fill_outside_bar" in _codes(_run(fills=above, market=slipped))
    below = (FILLS[0], replace(FILLS[1], price=D("107.99")))
    assert "fill_outside_bar" in _codes(_run(fills=below))
    no_bar = (replace(FILLS[0], day=D2), FILLS[1])
    market = _Market({k: v for k, v in BARS.items() if k[1] != D2}, MARKET.dividends)
    assert "fill_without_bar" in _codes(_run(fills=no_bar, market=market))


def test_negative_cash_is_caught() -> None:
    """The walked cash below zero at a close fails `negative_cash`. Harm: a run buying with
    money it does not have."""
    report = reconcile(D(900), BOOKS, FILLS, (TRADE,), MARKET)
    assert "negative_cash" in _codes(report)
    assert "negative_cash" not in _codes(_run())


def test_a_dividend_on_the_wrong_shares_is_caught() -> None:
    """Dividends are the cash per share times the shares the fills held into the ex-date: an
    engine crediting 5 shares' worth fails `dividends` (and its cash, the walk)."""
    books = (
        BOOKS[0],
        replace(BOOKS[1], dividends=D(10), cash=D("99008.81"), equity=D("100058.81")),
        replace(BOOKS[2], cash=D("100092.91"), equity=D("100092.91")),
    )
    assert {"dividends", "cash_walk", "equity"} <= _codes(_run(books=books))
    # bought on the ex-date: not entitled
    late = (replace(FILLS[0], day=D2), FILLS[1])
    market = _Market(BARS, {("abc", D2): D(2)})
    assert "dividends" in _codes(_run(fills=late, market=market))


def test_a_wrong_mark_is_caught() -> None:
    """A held position is marked at its bar's close: an engine marking it at 104 on D2 (close
    105) fails `position_value` and `equity`."""
    books = (BOOKS[0], replace(BOOKS[1], position_value=D(1040), equity=D("100058.81")), BOOKS[2])
    assert {"position_value", "equity"} <= _codes(_run(books=books))


def test_a_wrong_suspended_mark_is_caught() -> None:
    """Without a bar a position is marked at its last traded close, one band (5%) lower per
    session past `suspension_sessions`, to the tick: 102 then 96.90 after one session with
    none. An engine keeping 102 fails `position_value`."""
    bars = {k: v for k, v in BARS.items() if k[1] != D2}
    held = _Market(bars, MARKET.dividends, suspension_sessions=1)
    marked = _Market(bars, MARKET.dividends, suspension_sessions=0)
    books = (BOOKS[0], replace(BOOKS[1], position_value=D(1020), equity=D("100038.81")), BOOKS[2])
    assert "position_value" not in _codes(_run(books=books, market=held))
    assert "position_value" in _codes(_run(books=books, market=marked))
    good = (BOOKS[0], replace(BOOKS[1], position_value=D("969.0"), equity=D("99987.81")), BOOKS[2])
    assert "position_value" not in _codes(_run(books=good, market=marked))


def test_a_sale_without_a_bar_prints_between_the_marks() -> None:
    """A delisting or suspension exit on a day without a bar prints between the suspended mark
    and the last traded close (each after slippage), never above the close."""
    bars = {k: v for k, v in BARS.items() if k[1] != D3}  # D3: no bar; last close 105 on D2
    market = _Market(bars, MARKET.dividends, suspension_sessions=0)  # suspended mark 99.75
    at_close = (FILLS[0], replace(FILLS[1], price=D(105)))
    above = (FILLS[0], replace(FILLS[1], price=D(106)))
    below = (FILLS[0], replace(FILLS[1], price=D("99.70")))
    assert "fill_outside_bar" not in _codes(_run(fills=at_close, market=market))
    assert "fill_outside_bar" in _codes(_run(fills=above, market=market))
    assert "fill_outside_bar" in _codes(_run(fills=below, market=market))


def test_cash_off_by_more_than_a_rupee_fails() -> None:
    books = (BOOKS[0], replace(BOOKS[1], cash=D("99016.81"), equity=D("100066.81")), BOOKS[2])
    assert _codes(_run(books=books)) == {"cash_walk", "equity"}
    within = (BOOKS[0], replace(BOOKS[1], cash=D("99019.31"), equity=D("100069.31")), BOOKS[2])
    assert _run(books=within)["status"] == "pass"


def test_holdings_against_the_engine() -> None:
    lost = (BOOKS[0], replace(BOOKS[1], held=()), BOOKS[2])
    assert _codes(_run(books=lost)) == {"holdings"}
    unsold = _run(fills=FILLS[:1])
    assert {"holdings", "cash_walk", "instrument_pnl"} <= _codes(unsold)


def test_trade_pnl_and_dividends() -> None:
    wrong = replace(TRADE, realized_pnl=D(120))
    assert _codes(_run(trades=(wrong,))) == {"trade_pnl", "instrument_pnl"}
    unpaid = replace(TRADE, dividends=D(0), realized_pnl=D("82.91"))
    assert _codes(_run(trades=(unpaid,))) == {"trade_dividends"}
    stray = (*FILLS, FillRow(date(2024, 1, 4), "abc", "exit", "sell", 1, D(1), D(0)))
    assert "fill_outside_sessions" in _codes(_run(fills=stray))


def test_the_overlay_journal_is_walked() -> None:
    """The sleeve's fills are charged and walked like the account's, its distributed units
    valued at their close and its tax paid from the cash; a fill priced on an estimate is not
    range-checked."""
    bars = {**BARS, ("LIQUIDBEES", D1): (D(999), D(1001), D(1000))}
    bars |= {
        ("LIQUIDBEES", D2): (D(999), D(1001), D(1000)),
        ("LIQUIDBEES", D3): (D(999), D(1001), D(1000)),
    }
    market = _Market(bars, MARKET.dividends)
    buy = FillRow(D1, "LIQUIDBEES", "overlay", "buy", 5, D(1000), D("5.96"))
    assert charges(SCHEDULE.on(D1), "buy", 5, D(1000)).total == buy.fees
    journal = OverlayJournal(
        fills=(buy,), taxes=((D3, D(100)),), distributions=((D2, "LIQUIDBEES", 1),)
    )
    out = D("5005.96")
    books = (
        replace(
            BOOKS[0],
            cash=BOOKS[0].cash - out,
            equity=BOOKS[0].equity - D("5.96"),
            position_value=D(6020),
        ),
        replace(
            BOOKS[1],
            cash=BOOKS[1].cash - out,
            equity=BOOKS[1].equity + D("994.04"),
            position_value=D(7050),
        ),
        replace(
            BOOKS[2],
            cash=BOOKS[2].cash - out - 100,
            equity=BOOKS[2].equity + D("894.04"),
            position_value=D(6000),
        ),
    )
    # 6 units left after the period: the sleeve's units must be sold by then
    assert _codes(_run(books=books, market=market, journal=journal)) == {"holdings"}
    outside = replace(buy, price=D(1002))
    estimated = OverlayJournal(fills=(outside,), estimated=frozenset({0}))
    assert "fill_outside_bar" not in _codes(_run(market=market, journal=estimated))
    assert "fill_outside_bar" in _codes(
        _run(market=market, journal=OverlayJournal(fills=(outside,)))
    )


def test_a_trade_bought_in_several_fills_is_checked_by_its_instrument() -> None:
    # a rotation's top-up: 5 at 100 then 5 at 102, sold 10 at 110; the trade's entry price is
    # its first buy's, so only the instrument's P&L recomputes it
    fills = (
        FillRow(D1, "abc", "entry", "buy", 5, D(100), D("0.60")),
        FillRow(D2, "abc", "add", "buy", 5, D(102), D("0.61")),
        FillRow(D3, "abc", "exit", "sell", 10, D(110), D("15.90")),
    )
    books = (
        SessionBook(D1, D("100009.40"), D("99499.40"), D(510), (Held("abc", 5, D(102)),)),
        SessionBook(D2, D("100038.79"), D("98988.79"), D(1050), (Held("abc", 10, D(105)),)),
        SessionBook(D3, D("100072.89"), D("100072.89"), D(0)),
    )
    market = _Market(BARS)
    trade = replace(TRADE, realized_pnl=D("72.89"), fees=D("17.11"), dividends=D(0))
    assert reconcile(START, books, fills, (trade,), market)["status"] == "pass"
    wrong = replace(trade, realized_pnl=D(90))
    assert _codes(reconcile(START, books, fills, (wrong,), market)) == {"instrument_pnl"}


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
