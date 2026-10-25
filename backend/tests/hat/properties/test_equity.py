"""The account equity basis (Q1, owner 2026-10-25): available cash plus the shares at market,
never "Total Balance". A CNC buy's cost leaves the available cash on the trade date and sits in
"Utilized" until settlement; the shares are today's position on the trade date and a holding
from settlement. Each broker's replies on the day before, the trade date and the settlement day
give one equity: the buy is never counted out twice, nor its cost and its shares both in."""

from decimal import Decimal

import pytest

from app.hat.execution import fyers_payloads, kite_payloads
from app.hat.execution.broker import BrokerFunds, BrokerHolding, BrokerPosition
from app.hat.execution.equity import OpenCost, account_equity, book_equity

CASH = Decimal("100000")
QTY, COST = 10, Decimal("1500")  # the CNC buy: 15,000
BOUGHT = QTY * COST


def _fyers(available: Decimal, used: Decimal, positions: list, holdings: list) -> Decimal:
    funds = fyers_payloads.parse_funds(
        {
            "fund_limit": [
                {"title": "Total Balance", "equityAmount": float(available + used)},
                {"title": "Utilized Amount", "equityAmount": float(used)},
                {"title": "Available Balance", "equityAmount": float(available)},
            ]
        }
    )
    return account_equity(
        funds,
        fyers_payloads.parse_positions(positions),
        fyers_payloads.parse_holdings(holdings),
    )


def _fyers_position(ltp: Decimal) -> dict:
    return {
        "symbol": "NSE:TCS-EQ",
        "netQty": QTY,
        "buyAvg": float(COST),
        "productType": "CNC",
        "ltp": float(ltp),
    }


def _fyers_holding(ltp: Decimal) -> dict:
    return {"symbol": "NSE:TCS-EQ", "quantity": QTY, "costPrice": float(COST), "ltp": float(ltp)}


def test_fyers_equity_counts_a_cnc_buy_once_on_the_trade_date_and_at_settlement() -> None:
    before = _fyers(CASH, Decimal(0), [], [])
    trade_date = _fyers(CASH - BOUGHT, BOUGHT, [_fyers_position(COST)], [])
    settled = _fyers(CASH - BOUGHT, Decimal(0), [], [_fyers_holding(COST)])
    assert before == trade_date == settled == CASH
    # "Total Balance" would read 100,000 then 85,000 at settlement, plus the shares on top
    moved = _fyers(CASH - BOUGHT, Decimal(0), [], [_fyers_holding(Decimal("1600"))])
    assert moved == CASH + QTY * Decimal(100)


def _kite(net: Decimal, debits: Decimal, positions: list, holdings: list) -> Decimal:
    same = str
    funds = kite_payloads.parse_funds({"net": float(net), "utilised": {"debits": float(debits)}})
    return account_equity(
        funds,
        kite_payloads.parse_positions({"net": positions}, same),
        kite_payloads.parse_holdings(holdings, same),
    )


def test_kite_equity_follows_the_same_basis() -> None:
    position = {
        "tradingsymbol": "TCS",
        "quantity": QTY,
        "average_price": float(COST),
        "product": "CNC",
        "last_price": float(COST),
    }
    t1 = {
        "tradingsymbol": "TCS",
        "quantity": 0,
        "t1_quantity": QTY,
        "average_price": float(COST),
        "last_price": float(COST),
    }
    delivered = {**t1, "quantity": QTY, "t1_quantity": 0}
    before = _kite(CASH, Decimal(0), [], [])
    trade_date = _kite(CASH - BOUGHT, BOUGHT, [position], [])
    t1_day = _kite(CASH - BOUGHT, Decimal(0), [], [t1])
    settled = _kite(CASH - BOUGHT, Decimal(0), [], [delivered])
    assert before == trade_date == t1_day == settled == CASH


def test_a_sale_today_nets_the_holding_out() -> None:
    """Shares sold today are a holding and a negative net position: none left to value."""
    funds = BrokerFunds(available=CASH + BOUGHT, used=Decimal(0))
    holding = BrokerHolding("TCS", QTY, COST, Decimal("1500"))
    sold = BrokerPosition("TCS", -QTY, COST, "CNC", Decimal("1500"))
    assert account_equity(funds, [sold], [holding]) == CASH + BOUGHT


def test_shares_without_a_price_fail_the_read() -> None:
    funds = BrokerFunds(available=CASH, used=Decimal(0))
    with pytest.raises(ValueError):
        account_equity(funds, [], [BrokerHolding("TCS", QTY, COST)])
    # an intraday position is not a share held
    intraday = BrokerPosition("SBIN", 5, Decimal("800"), "INTRADAY")
    assert account_equity(funds, [intraday], []) == CASH


def test_book_equity_holds_hats_open_positions_at_cost() -> None:
    """The allocator's input: HAT's open position at its cost on both days, whatever its price,
    so the drawdown's own mark (fill cash plus the shares marked) adds its result once. The
    owner's own shares count at market."""
    hat = OpenCost("TCS", QTY, -BOUGHT)
    own = BrokerHolding("INFY", 4, Decimal("1400"), Decimal("1450"))
    trade_date = book_equity(
        BrokerFunds(available=CASH - BOUGHT, used=BOUGHT),
        [BrokerPosition("TCS", QTY, COST, "CNC", Decimal("1700"))],
        [own],
        [hat],
    )
    settled = book_equity(
        BrokerFunds(available=CASH - BOUGHT, used=Decimal(0)),
        [],
        [BrokerHolding("TCS", QTY, COST, Decimal("1650")), own],
        [hat],
    )
    assert trade_date == settled == CASH + 4 * Decimal("1450")
