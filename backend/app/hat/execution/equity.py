"""The account equity HAT allocates on (Q1, owner 2026-10-25), from one read of the broker's
funds, positions and holdings.

Equity is the cash available to trade plus the shares held at market: the holdings plus today's
net CNC position (`broker_quantities`), each at the broker's last traded price. It is not the
funds' "Total Balance": a CNC buy's cost leaves the available cash on the trade date and sits in
"Utilized" until settlement, so a total read counts it as cash on the trade date and drops it
when it settles, while the shares were bought on the trade date. Here the buy moves cash into
shares on the trade date (the shares in today's position) and settlement changes nothing (the
same shares, now in the holdings; the cash already out), so neither day counts it twice.

`book_equity` is what the allocator takes: HAT's own open positions enter it at their cost, since
the drawdown marks them itself (`kill.open_pnl`) on top of the allocated capital.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from .broker import BrokerFunds, BrokerHolding, BrokerPosition
from .reconcile import broker_quantities

__all__ = ["OpenCost", "account_equity", "book_equity"]


@dataclass(frozen=True)
class OpenCost:
    """One of HAT's open real positions on the account: its symbol, the shares it holds and the
    net cash of its fills (sales less purchases, less fees: negative while held)."""

    symbol: str
    qty: int
    cash: Decimal


def _prices(
    positions: Iterable[BrokerPosition], holdings: Iterable[BrokerHolding]
) -> dict[str, Decimal]:
    """Each symbol's last traded price as the broker gave it, a holding's first."""
    prices: dict[str, Decimal] = {}
    for row in [*holdings, *positions]:
        if row.ltp is not None:
            prices.setdefault(row.symbol, row.ltp)
    return prices


def _price(prices: dict[str, Decimal], symbol: str) -> Decimal:
    found = prices.get(symbol)
    if found is None:
        raise ValueError(f"{symbol}: no last traded price")
    return found


def account_equity(
    funds: BrokerFunds, positions: list[BrokerPosition], holdings: list[BrokerHolding]
) -> Decimal:
    """The available cash plus every share held (holdings plus today's net CNC position) at its
    last traded price. Raises ValueError for shares without a price or a short net quantity: an
    equity that leaves out shares is not one."""
    prices = _prices(positions, holdings)
    equity = funds.available
    for symbol, qty in broker_quantities(positions, holdings).items():
        if qty < 0:
            raise ValueError(f"{symbol}: short {qty}")
        if qty:
            equity += qty * _price(prices, symbol)
    return equity


def book_equity(
    funds: BrokerFunds,
    positions: list[BrokerPosition],
    holdings: list[BrokerHolding],
    hat_open: Iterable[OpenCost],
) -> Decimal:
    """`account_equity` less the open P&L of HAT's open positions at the same prices (each one's
    fill cash plus its shares at the price, as `kill.open_pnl` marks it), so those shares count
    at their cost and the drawdown's own mark adds their result once. Raises ValueError as
    `account_equity`, and for an open position whose symbol has no price."""
    prices = _prices(positions, holdings)
    equity = account_equity(funds, positions, holdings)
    for position in hat_open:
        if position.qty:
            equity -= position.cash + position.qty * _price(prices, position.symbol)
    return equity
