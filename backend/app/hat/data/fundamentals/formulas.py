"""Every derived fundamental's formula, once. Pure Decimal arithmetic: a formula whose input is
missing, or whose denominator is zero (or, where stated, not positive), is None, never a guess.
docs/fundamentals.md lists the same formulas with the items they read.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

DAYS_A_YEAR = Decimal(365)
TWO = Decimal(2)

D = Decimal | None


def total(values: Iterable[D]) -> D:
    """The sum, None when any part is missing (a TTM of four quarters)."""
    out = Decimal(0)
    for value in values:
        if value is None:
            return None
        out += value
    return out


def some_total(values: Iterable[D]) -> D:
    """The sum of the parts present, None when none is (borrowings split over several lines)."""
    present = [v for v in values if v is not None]
    return sum(present, Decimal(0)) if present else None


def ratio(numerator: D, denominator: D) -> D:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def positive_ratio(numerator: D, denominator: D) -> D:
    """`ratio` defined only for a positive denominator (P/E on a loss, ROE on negative equity
    are undefined)."""
    if denominator is None or denominator <= 0:
        return None
    return ratio(numerator, denominator)


def average(current: D, prior: D) -> D:
    """The mean of a balance now and a year earlier; the current balance alone when the earlier
    one is not known."""
    if current is None:
        return None
    return current if prior is None else (current + prior) / TWO


def growth(current: D, prior: D) -> D:
    """current / prior - 1, for a positive prior (growth from a loss is undefined)."""
    if current is None or prior is None or prior <= 0:
        return None
    return current / prior - 1


def cagr(end: D, start: D, years: int) -> D:
    """(end / start) ^ (1 / years) - 1, for a positive start and end."""
    if end is None or start is None or end <= 0 or start <= 0 or years <= 0:
        return None
    return ((end / start).ln() / years).exp() - 1


def owners_profit(owners: D, period: D) -> D:
    """The profit attributable to the owners of the parent where tagged, else the period's
    profit; a template owners' field left at 0 while the period shows a profit is not the
    owners' share (`results/xbrl.py`)."""
    if owners is not None and (owners != 0 or not period):
        return owners
    return period


def operating_profit(revenue: D, total_expenses: D, finance_cost: D, depreciation: D) -> D:
    """EBITDA before other income: revenue - (total expenses - finance cost - depreciation)."""
    if revenue is None or total_expenses is None:
        return None
    return revenue - (total_expenses - (finance_cost or 0) - (depreciation or 0))


def cost_of_goods(material: D, purchases: D, inventory_change: D) -> D:
    """Materials consumed + purchases of stock in trade + change in inventories."""
    return some_total((material, purchases, inventory_change))


def ebit(pbt: D, finance_cost: D) -> D:
    """Profit before tax + finance cost."""
    if pbt is None:
        return None
    return pbt + (finance_cost or 0)


def equity(owners: D, total_equity: D, minority: D, share_capital: D, reserves: D) -> D:
    """Equity attributable to the owners: as tagged; else total equity less non-controlling
    interest; else share capital + reserves."""
    if owners is not None:
        return owners
    if total_equity is not None:
        return total_equity - (minority or 0)
    if share_capital is None or reserves is None:
        return None
    return share_capital + reserves


def borrowings(stated: D, current: D, noncurrent: D, debt_securities: D, subordinated: D) -> D:
    """Total borrowings: the stated total (a bank's, an NBFC's, the secondary source's) else
    current + non-current; plus an NBFC's debt securities and subordinated liabilities."""
    base = stated if stated is not None else some_total((current, noncurrent))
    extra = some_total((debt_securities, subordinated))
    if base is None:
        return extra
    return base + (extra or 0)


def capital_employed(equity_value: D, borrowings_value: D) -> D:
    """Equity + borrowings (the secondary source's definition)."""
    if equity_value is None:
        return None
    return equity_value + (borrowings_value or 0)


def days(balance: D, annual_flow: D) -> D:
    """A balance as days of a year's flow: balance / flow * 365 (receivables on revenue,
    inventories and payables on cost of goods)."""
    value = positive_ratio(balance, annual_flow)
    return None if value is None else value * DAYS_A_YEAR


def free_cash_flow(cfo: D, capex: D) -> D:
    """Cash from operations - capital expenditure (purchases of property, plant, equipment and
    intangibles, stated as positive outflows)."""
    if cfo is None or capex is None:
        return None
    return cfo - abs(capex)


def shares(paid_up_capital: D, face_value: D) -> D:
    """Shares outstanding: paid-up equity capital / face value."""
    return positive_ratio(paid_up_capital, face_value)


def split_multiplier(price_factors: Iterable[Decimal]) -> Decimal:
    """How many shares one share became through splits and bonuses with these price factors
    (a 1:1 bonus halves the price, factor 0.5, and doubles the shares): the product of
    1 / factor."""
    out = Decimal(1)
    for factor in price_factors:
        out /= factor
    return out


def market_cap(share_count: D, price: D) -> D:
    if share_count is None or price is None:
        return None
    return share_count * price


def dividends(paid: D, payout: D, annual_profit: D) -> D:
    """The year's dividends: as paid in the cash flow statement, else the secondary source's
    payout ratio times the year's profit."""
    if paid is not None:
        return abs(paid)
    if payout is None or annual_profit is None:
        return None
    return payout * annual_profit
