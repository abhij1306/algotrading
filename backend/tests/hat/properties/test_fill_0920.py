"""The price an order may fill at no earlier than 09:20 (`engine/fill_0920.py`), on hand-built
five-minute bars: the 09:15 bar's close, the open moved by the median without it, and which one
was used."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from app.hat.data.bars import Bar
from app.hat.engine.fill_0920 import (
    ESTIMATED,
    REAL,
    OpenTo0920,
    estimated_0920,
    fill_0920,
    gap_over_fill,
    price_0920,
)
from app.hat.time import IST

D = Decimal
DAY = date(2024, 2, 1)


def _bar(minute: int, open_: str, close: str, high: str | None = None) -> Bar:
    start = datetime.combine(DAY, time(9, 15), tzinfo=IST) + timedelta(minutes=minute)
    top = high or max(open_, close, key=D)
    return Bar("i", start, D(open_), D(top), min(D(open_), D(close)), D(close), 100)


def test_the_price_at_0920_is_the_0915_bars_close() -> None:
    bars = [_bar(0, "100", "101.5"), _bar(5, "101.5", "103")]
    found = price_0920(DAY, bars)
    assert found is not None
    assert found[0] == D("101.5")  # not the 09:15 open, not the 09:20 bar's open or close
    assert found[1] is bars[0]


def test_no_0915_bar_no_price_at_0920() -> None:
    assert price_0920(DAY, [_bar(5, "101", "102")]) is None  # the session starts late
    assert price_0920(DAY, []) is None


def test_the_estimate_moves_the_open_against_the_order() -> None:
    median = OpenTo0920(buy=D("0.002"), sell=D("0.003"))
    assert estimated_0920(D(100), "buy", median) == D("100.2000")
    assert estimated_0920(D(100), "sell", median) == D("99.7000")


def test_fill_0920_uses_the_bar_and_falls_back_to_the_estimate() -> None:
    median = OpenTo0920(buy=D("0.001"), sell=D("0.001"))
    bars = (_bar(0, "100", "101"),)
    assert fill_0920(DAY, "buy", bars, D(100), median) == (D(101), REAL)
    assert fill_0920(DAY, "sell", bars, D(100), median) == (D(101), REAL)
    # no five-minute session the caller may read (none stored, refused, a late start)
    assert fill_0920(DAY, "buy", None, D(100), median) == (D("100.1000"), ESTIMATED)
    assert fill_0920(DAY, "sell", (), D(100), median) == (D("99.9000"), ESTIMATED)
    late = (_bar(5, "100", "101"),)
    assert fill_0920(DAY, "buy", late, D(100), median) == (D("100.1000"), ESTIMATED)


def test_a_gap_over_fills_at_the_estimate_never_above_the_high() -> None:
    median = OpenTo0920(buy=D("0.01"), sell=D(0))
    assert gap_over_fill(D(100), D(105), median) == D("101.0000")
    assert gap_over_fill(D(100), D("100.5"), median) == D("100.5")  # nothing traded above


def test_the_estimate_stays_within_the_days_range() -> None:
    median = OpenTo0920(buy=D("0.01"), sell=D("0.01"))
    # opened at its high: a buy pays at most the high; opened at its low: a sale gets the low
    assert estimated_0920(D(100), "buy", median, low=D(97), high=D(100)) == D(100)
    assert estimated_0920(D(100), "sell", median, low=D(100), high=D(103)) == D(100)
    assert estimated_0920(D(100), "buy", median, low=D(97), high=D(103)) == D("101.0000")
    found = fill_0920(DAY, "sell", None, D(100), median, low=D("99.5"), high=D(101))
    assert found == (D("99.5"), ESTIMATED)


def test_a_sale_is_refused_on_a_0915_bar_closing_locked_at_the_lower_band() -> None:
    from app.hat.backtest.fillrules import close_lower_locked, lower_circuit_locked

    # previous close 100, a 5% band: lower price 95. The bar traded a tick above, closed at 95.
    bar = _bar(0, "95.05", "95", high="95.05")
    tick = D("0.05")
    assert close_lower_locked(bar.close, bar.low, D(100), tick) is True  # refused at 09:20
    # the one-price rule alone would have sold it at the band
    assert lower_circuit_locked((bar.open, bar.high, bar.low, bar.close), D(100), tick) is False
