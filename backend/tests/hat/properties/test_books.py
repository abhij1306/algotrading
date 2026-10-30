"""The live and paper books' rules (`daily.books`): the curve rebuilt from fills, dividends and
closes, an open position's mark, and the report on the backtest's own metric code."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from app.hat.backtest.standard import BookSession, BookTrade, book_metrics
from app.hat.daily.books import (
    BookFill,
    ClosedTrade,
    book_curve,
    book_report,
    books_text,
    open_row,
    report_doc,
)
from app.hat.engine.trade_dividends import Step, dividend_credits
from app.hat.time import IST

D = Decimal
DAYS = [date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29)]
START = D("100000")


def test_curve_counts_cash_shares_and_dividends_on_their_sessions() -> None:
    fills = [
        BookFill(DAYS[0], "a", 100, D("100"), D("20")),  # bought: cash -10020
        BookFill(DAYS[2], "a", -100, D("110"), D("22")),  # sold: cash +10978
    ]
    closes = {"a": [(DAYS[0], D("101")), (DAYS[1], D("105"))]}  # no bar on DAYS[2]
    curve = book_curve(START, DAYS, fills, [(DAYS[3], D("50"))], closes)
    assert [(s.equity, s.position_value, s.open_positions) for s in curve] == [
        (D("100080"), D("10100"), 1),  # 100000 - 10020 + 100 x 101
        (D("100480"), D("10500"), 1),
        (D("100958"), D("0"), 0),  # flat: the sale's net
        (D("101008"), D("0"), 0),  # the dividend on its session
    ]


def test_curve_marks_a_stock_without_a_close_at_its_last_fill() -> None:
    fills = [BookFill(DAYS[1], "b", 10, D("50"), D("0"))]
    curve = book_curve(START, DAYS[:2], fills, [], {})
    assert curve[-1] == BookSession(DAYS[1], START, D("500"), 1)


def test_open_row_marks_costs_and_open_r() -> None:
    row = open_row(
        symbol="ABC",
        qty=100,
        avg_entry=D("100"),
        initial_stop=D("95"),
        current_stop=D("98"),
        opened=DAYS[0],
        fill_cash=D("-10020"),
        mark=D("110"),
    )
    assert (row.value, row.unrealised, row.open_r) == (D("11000"), D("980"), D("2"))
    unmarked = open_row(
        symbol="ABC",
        qty=100,
        avg_entry=D("100"),
        initial_stop=D("100"),
        current_stop=D("100"),
        opened=DAYS[0],
        fill_cash=D("-10020"),
        mark=None,
    )
    assert (unmarked.value, unmarked.unrealised, unmarked.open_r) == (D("10000"), D("-20"), None)


def test_report_uses_the_backtest_metrics_on_the_same_inputs() -> None:
    fills = [
        BookFill(DAYS[0], "a", 100, D("100"), D("20")),
        BookFill(DAYS[2], "a", -100, D("110"), D("22")),
        BookFill(DAYS[3], "b", 10, D("200"), D("4")),
    ]
    closes = {"a": [(DAYS[0], D("101")), (DAYS[1], D("105"))], "b": [(DAYS[3], D("210"))]}
    trade = ClosedTrade("A", DAYS[0], DAYS[2], 2, "trail_step", D("958"), D("42"), D("1.9"))
    bench = [(date(2026, 10, 23), D("1000")), *((d, D("1000") + n) for n, d in enumerate(DAYS))]
    report = book_report(
        bot="s1",
        kind="paper",
        start=START,
        period_start=DAYS[0],
        days=DAYS,
        fills=fills,
        dividends=[],
        closes=closes,
        trades=[trade],
        positions=[],
        benchmark=bench,
    )
    curve = book_curve(START, DAYS, fills, [], closes)
    assert report.metrics == book_metrics(
        [BookTrade("A", D("1.9"), D("958"), D("42"), 2, DAYS[2])],
        [(s.day, s.equity) for s in curve],
        curve,
        START,
        DAYS[0],
        bench,
    )
    assert report.equity == D("100958") - D("2004") + D("2100")
    assert report.day_pnl == report.equity - D("100958")
    assert (report.realised, report.realised_today) == (D("958"), D("0"))
    assert (report.fees, report.fees_today) == (D("46"), D("4"))
    assert report.position_value == D("2100")
    assert report.invested_pct == round(float(D("2100") / report.equity * 100), 4)
    assert report.benchmark_return_pct == 0.3  # 1000 -> 1003
    assert report.metrics["trades"] == 1
    assert report.metrics["expectancy_r"] == 1.9


def test_text_and_json_show_every_book() -> None:
    report = book_report(
        bot="s1",
        kind="live",
        start=START,
        period_start=DAYS[0],
        days=DAYS,
        fills=[],
        dividends=[],
        closes={},
        trades=[ClosedTrade("A", DAYS[0], DAYS[3], 3, "sma50_close", D("-500"), D("40"), D("-1"))],
        positions=[],
        benchmark=[],
    )
    text = books_text(DAYS[3], [report])
    assert "s1 live (since 2026-10-26, start 100000.00)" in text
    assert "closed A 2026-10-26..2026-10-29 -1.00R -500.00 (sma50_close)" in text
    assert "vs NIFTY 500 TRI n/a" in text
    doc = report_doc([report])
    assert doc[0]["equity"] == "100000"
    assert doc[0]["trades"][0]["r"] == "-1"
    assert books_text(DAYS[3], []).splitlines()[-1] == "No book: no bot has an allocation yet."


def test_loss_streak_is_counted_in_exit_order() -> None:
    """Entered A (loss), B (win), C (loss); B exits last, so the two losses are a streak."""

    def trade(r: str, entry: int, exit_: int) -> BookTrade:
        return BookTrade("X", D(r), D(r) * 100, D("1"), exit_ - entry, DAYS[exit_])

    trades = [trade("-1", 0, 1), trade("2", 0, 3), trade("-1", 1, 2)]
    curve = [(d, START) for d in DAYS]
    sessions = [BookSession(d, START, D(0), 0) for d in DAYS]
    metrics = book_metrics(trades, curve, sessions, START, DAYS[0])
    assert metrics["max_consecutive_losses"] == 2


def test_dividends_are_credited_on_their_ex_dates() -> None:
    """A position's dividends land on each ex-date it held shares at the open (the backtest's
    rule), not at its exit: the curve moves on the ex-date."""

    def at(day: date) -> datetime:
        return datetime.combine(day, time(10, 0), tzinfo=IST)

    steps = [Step(at(DAYS[0]), "buy", 100), Step(at(DAYS[2]), "sell", 100)]
    paid = [(DAYS[0], D("5")), (DAYS[1], D("2")), (DAYS[3], D("9"))]
    # bought on DAYS[0] (its ex-date earns nothing), sold on DAYS[2]; DAYS[3] past `through`
    assert dividend_credits(steps, paid, DAYS[2]) == [(DAYS[1], D("200"))]
    curve = book_curve(START, DAYS, [], [(DAYS[1], D("200"))], {})
    assert [s.equity for s in curve] == [START, START + 200, START + 200, START + 200]
