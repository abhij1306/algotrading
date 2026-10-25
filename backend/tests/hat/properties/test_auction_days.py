"""The sessions whose auction close is far from live's pre-15:15 price, and the exits flagged
on them (`backtest/auction_days.py`, `report.flag_auction_risk`; owner 2026-10-25)."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date, timedelta
from decimal import Decimal

from app.hat.backtest.auction_days import (
    auction_risk_days,
    index_rebalance_closes,
    monthly_expiries,
)
from app.hat.backtest.report import TradeRow, auction_risk_summary, flag_auction_risk

D = Decimal


def weekdays(start: date, end: date, closed: tuple[date, ...] = ()) -> list[date]:
    days = (start + timedelta(days=n) for n in range((end - start).days + 1))
    return [d for d in days if d.weekday() < 5 and d not in closed]


def test_monthly_expiry_moves_from_last_thursday_to_last_tuesday() -> None:
    sessions = weekdays(date(2025, 7, 1), date(2025, 10, 10))
    # last Thursday through August 2025, last Tuesday from September; October not yet reached
    assert monthly_expiries(sessions) == {date(2025, 7, 31), date(2025, 8, 28), date(2025, 9, 30)}


def test_an_expiry_on_a_holiday_moves_to_the_previous_session() -> None:
    sessions = weekdays(date(2025, 8, 1), date(2025, 9, 5), closed=(date(2025, 8, 28),))
    assert monthly_expiries(sessions) == {date(2025, 8, 27)}


def test_the_rebalance_close_is_the_session_before_the_last_of_march_and_september() -> None:
    sessions = weekdays(date(2025, 3, 1), date(2025, 10, 3), closed=(date(2025, 3, 31),))
    assert index_rebalance_closes(sessions) == {date(2025, 3, 27), date(2025, 9, 29)}
    # a period that ends inside September cannot tell its last session yet
    assert index_rebalance_closes(weekdays(date(2025, 9, 1), date(2025, 9, 30))) == frozenset()


def test_both_tags_on_one_day() -> None:
    # March 2025: expiry Thursday 27th, and the 27th is the session before the last (the 28th)
    days = auction_risk_days(weekdays(date(2025, 3, 1), date(2025, 4, 4), (date(2025, 3, 31),)))
    assert days == {date(2025, 3, 27): "fo_expiry,index_rebalance"}


def _trade(exit_: date, reason: str, r: str) -> TradeRow:
    return TradeRow(
        entry_date=exit_ - timedelta(days=5),
        exit_date=exit_,
        playbook="pb",
        instrument_id="i",
        qty=10,
        entry_price=D(100),
        initial_stop=D(95),
        exit_reason=reason,
        realized_pnl=D(0),
        fees=D(0),
        r_multiple=D(r),
        mfe_r=D(0),
        mae_r=D(0),
        holding_days=5,
        strategy_version_id=uuid.UUID(int=1),
        risk_policy_version_id=uuid.UUID(int=2),
    )


def test_only_close_priced_exits_on_flagged_days_are_flagged_and_counted() -> None:
    expiry, rebalance, plain = date(2025, 9, 30), date(2025, 9, 29), date(2025, 9, 26)
    days = {expiry: "fo_expiry", rebalance: "index_rebalance"}
    trades = flag_auction_risk(
        (
            _trade(expiry, "close_below_sma", "1.5"),
            _trade(rebalance, "time_stop", "-0.5"),
            _trade(expiry, "hard_stop", "-1"),  # a stop fills at its own price
            _trade(plain, "close_below_sma", "2"),
        ),
        days,
    )
    assert [t.auction_risk for t in trades] == ["fo_expiry", "index_rebalance", None, None]
    summary = auction_risk_summary(trades)
    assert summary["exits"] == 2
    assert summary["by_tag"] == {"fo_expiry": 1, "index_rebalance": 1}
    assert D(summary["sum_r"]) == D(1)
    assert dataclasses.replace(trades[0], auction_risk=None) == _trade(
        expiry, "close_below_sma", "1.5"
    )
