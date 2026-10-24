"""The cash a shadow position earns on an ex-date (CA4, `engine/paper_dividends.py`): the shares
held at the start of the session times the cash per share, the backtest's rule. Toy positions;
no database."""

from datetime import date, datetime
from decimal import Decimal

from app.hat.engine.paper_dividends import (
    BUY,
    RESCALE,
    SELL,
    Step,
    held_at_start,
    position_dividends,
)
from app.hat.time import IST


def _at(day: int, hour: int = 10) -> datetime:
    return datetime(2026, 7, day, hour, 0, tzinfo=IST)


# bought 100 on the 6th, sold 40 on the 9th at 10:00, the rest on the 14th
TRADE = [Step(_at(6), BUY, 100), Step(_at(9), SELL, 40), Step(_at(14), SELL, 60)]


def test_shares_held_at_the_start_of_a_session() -> None:
    assert held_at_start(TRADE, date(2026, 7, 6)) == 0  # a buy on the day earns nothing
    assert held_at_start(TRADE, date(2026, 7, 7)) == 100
    assert held_at_start(TRADE, date(2026, 7, 9)) == 100  # a sale on the day keeps the credit
    assert held_at_start(TRADE, date(2026, 7, 10)) == 60
    assert held_at_start(TRADE, date(2026, 7, 15)) == 0


def test_a_position_held_into_an_ex_date_earns_its_dividends() -> None:
    dividends = [
        (date(2026, 7, 6), Decimal(9)),  # ex on the buy day: nothing
        (date(2026, 7, 9), Decimal("2.50")),  # 100 shares at the open: Rs 250
        (date(2026, 7, 10), Decimal("0.333")),  # 60 shares: Rs 19.98
        (date(2026, 7, 20), Decimal(5)),  # after the exit: nothing
    ]
    assert position_dividends(TRADE, dividends, date(2026, 7, 31)) == Decimal("269.98")
    # an ex-date after `through` is not paid yet
    assert position_dividends(TRADE, dividends, date(2026, 7, 9)) == Decimal("250.00")


def test_a_rescale_sets_the_shares_a_later_dividend_pays() -> None:
    # 1:2 split before the open of the 8th: 100 shares become 200
    steps = [Step(_at(6), BUY, 100), Step(_at(8, 8), RESCALE, 200)]
    assert held_at_start(steps, date(2026, 7, 8)) == 100
    assert position_dividends(steps, [(date(2026, 7, 9), Decimal(1))], date(2026, 7, 9)) == 200
