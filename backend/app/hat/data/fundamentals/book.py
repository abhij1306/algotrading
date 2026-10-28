"""One instrument's fundamentals as known at a moment, and the metrics derived from them. Pure.

`Book` holds the canonical facts of the filings public so far: a later version of a filing
(same source, basis and period end) replaces its predecessor's facts whole, and for each (basis,
period kind, period end, item) an NSE value wins over the secondary source's. `metrics` reads one
basis: consolidated when the latest quarter has consolidated figures, else standalone.

Quarter flows: the quarter's own figure, else for a year end the year less the nine months (a Q4
document with only the year). TTM: the four quarters to the latest quarter, else that year's
figure when the latest quarter ends a year. Balances: the latest balance sheet (a half-year or
year end) and the one a year before it, averaged where a formula takes an average. Valuation
takes the caller's price (the last close known at the moment) and the latest shares outstanding.
Formulas: `formulas.py`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from . import formulas as f
from .items import ANNUAL, INSTANT, QUARTER, YTD

NSE = "nse"
SCREENER = "screener"
CONSOLIDATED = "consolidated"
STANDALONE = "standalone"
CAGR_YEARS = (3, 5, 10)

D = Decimal | None
Cell = tuple[str, str, date, str]  # (basis, period kind, period end, item)
FilingKey = tuple[str, str, date]  # (source, basis, period end)


@dataclass(frozen=True)
class KnownFact:
    period_kind: str
    period_end: date
    item: str
    value: Decimal


def month_end(day: date) -> date:
    following = date(day.year + day.month // 12, day.month % 12 + 1, 1)
    return date.fromordinal(following.toordinal() - 1)


def months_before(end: date, months: int) -> date:
    """The month end `months` months before the month end `end`."""
    index = end.year * 12 + end.month - 1 - months
    return month_end(date(index // 12, index % 12 + 1, 1))


class Book:
    def __init__(self) -> None:
        self.cells: dict[Cell, dict[str, Decimal]] = {}
        self.filed: dict[FilingKey, list[Cell]] = {}
        self.ends: dict[tuple[str, str, str], set[date]] = defaultdict(set)
        self.family = ""

    def add(self, source: str, basis: str, period_end: date, facts: Iterable[KnownFact]) -> None:
        """A filing's facts, replacing those of the earlier version of the same filing."""
        key = (source, basis, period_end)
        for cell in self.filed.pop(key, []):
            self.cells.get(cell, {}).pop(source, None)
        added = []
        for fact in facts:
            cell = (basis, fact.period_kind, fact.period_end, fact.item)
            self.cells.setdefault(cell, {})[source] = fact.value
            self.ends[(basis, fact.period_kind, fact.item)].add(fact.period_end)
            added.append(cell)
        self.filed[key] = added

    def get(self, basis: str, kind: str, end: date | None, item: str) -> D:
        if end is None:
            return None
        values = self.cells.get((basis, kind, end, item))
        if not values:
            return None
        return values.get(NSE, values.get(SCREENER))

    def latest_end(self, basis: str, kind: str, items: Iterable[str], until: date) -> date | None:
        found = [
            e
            for item in items
            for e in self.ends.get((basis, kind, item), ())
            if e <= until and self.get(basis, kind, e, item) is not None
        ]
        return max(found, default=None)

    # --- series ---------------------------------------------------------------------------

    def quarter(self, basis: str, end: date, item: str) -> D:
        """The quarter's figure, else (a year end) the year less the nine months."""
        value = self.get(basis, QUARTER, end, item)
        if value is not None:
            return value
        year = self.get(basis, ANNUAL, end, item)
        nine = self.get(basis, YTD, months_before(end, 3), item)
        return None if year is None or nine is None else year - nine

    def ttm(self, basis: str, end: date, item: str) -> D:
        quarters = f.total(self.quarter(basis, months_before(end, 3 * n), item) for n in range(4))
        return quarters if quarters is not None else self.get(basis, ANNUAL, end, item)


@dataclass(frozen=True)
class Metric:
    name: str
    value: Decimal
    period_end: date  # the quarter, year or balance sheet the value reflects
    basis: str


FLOW_ITEMS = ("revenue", "net_profit")
BALANCE_ITEMS = ("total_assets", "equity", "equity_owners", "share_capital")
PLACES = Decimal("0.000001")


def choose_basis(book: Book, until: date) -> str:
    latest = {b: book.latest_end(b, QUARTER, FLOW_ITEMS, until) for b in (CONSOLIDATED, STANDALONE)}
    consolidated, standalone = latest[CONSOLIDATED], latest[STANDALONE]
    if consolidated is not None and (standalone is None or consolidated >= standalone):
        return CONSOLIDATED
    return STANDALONE


class Reader:
    """The metric inputs of one book, basis and moment."""

    def __init__(self, book: Book, basis: str) -> None:
        self.book = book
        self.basis = basis

    def q(self, end: date, item: str) -> D:
        return self.book.quarter(self.basis, end, item)

    def ttm(self, end: date, item: str) -> D:
        return self.book.ttm(self.basis, end, item)

    def at(self, kind: str, end: date | None, item: str) -> D:
        return self.book.get(self.basis, kind, end, item)

    def profit_q(self, end: date) -> D:
        return f.owners_profit(self.q(end, "net_profit_owners"), self.q(end, "net_profit"))

    def profit_ttm(self, end: date) -> D:
        return f.owners_profit(self.ttm(end, "net_profit_owners"), self.ttm(end, "net_profit"))

    def profit_annual(self, end: date | None) -> D:
        return f.owners_profit(
            self.at(ANNUAL, end, "net_profit_owners"), self.at(ANNUAL, end, "net_profit")
        )

    def op_q(self, end: date) -> D:
        stated = f.operating_profit(
            self.q(end, "revenue"),
            self.q(end, "total_expenses"),
            self.q(end, "finance_cost"),
            self.q(end, "depreciation"),
        )
        return stated if stated is not None else self.q(end, "operating_profit")

    def equity(self, end: date | None) -> D:
        return f.equity(
            self.at(INSTANT, end, "equity_owners"),
            self.at(INSTANT, end, "equity"),
            self.at(INSTANT, end, "minority_interest"),
            self.at(INSTANT, end, "share_capital"),
            self.at(INSTANT, end, "reserves"),
        )

    def borrowings(self, end: date | None) -> D:
        return f.borrowings(
            self.at(INSTANT, end, "borrowings"),
            self.at(INSTANT, end, "borrowings_current"),
            self.at(INSTANT, end, "borrowings_noncurrent"),
            self.at(INSTANT, end, "debt_securities"),
            self.at(INSTANT, end, "subordinated_liabilities"),
        )

    def cogs_ttm(self, end: date) -> D:
        return f.cost_of_goods(
            self.ttm(end, "material_cost"),
            self.ttm(end, "purchases_stock"),
            self.ttm(end, "inventory_change"),
        )


@dataclass(frozen=True)
class Quote:
    """The last close known at a moment, and the instrument's splits and bonuses (ex-date, price
    factor) for carrying a share count forward to that close."""

    day: date
    close: Decimal
    splits: tuple[tuple[date, Decimal], ...] = ()


def metrics(book: Book, until: date, quote: Quote | None = None) -> list[Metric]:
    """The metrics the book gives for periods ending by `until` (the moment's date), with the
    last close `quote` for valuation; each metric only where defined."""
    return _Derivation(book, until, quote).run()


class _Derivation:
    def __init__(self, book: Book, until: date, quote: Quote | None) -> None:
        self.book = book
        self.until = until
        self.quote = quote
        self.basis = choose_basis(book, until)
        self.r = Reader(book, self.basis)
        self.banking = book.family == "bank"
        self.out: list[Metric] = []
        self.qe = book.latest_end(self.basis, QUARTER, FLOW_ITEMS, until)
        self.ye = book.latest_end(self.basis, ANNUAL, FLOW_ITEMS, until)
        self.be = book.latest_end(self.basis, INSTANT, BALANCE_ITEMS, until)
        self.ttm_revenue: D = None
        self.ttm_profit: D = None

    def put(self, name: str, value: D, end: date | None) -> None:
        if value is not None and end is not None:
            self.out.append(Metric(name, value.quantize(PLACES), end, self.basis))

    def run(self) -> list[Metric]:
        if self.qe is None and self.ye is None and self.be is None:
            return self.out
        if self.qe is not None:
            self.quarters(self.qe)
        if self.ye is not None:
            self.years(self.ye)
            self.cash_flow()
        if self.be is not None:
            self.balances(self.be)
        self.valuation()
        return self.out

    def quarters(self, qe: date) -> None:
        r, put = self.r, self.put
        self.ttm_revenue = r.ttm(qe, "revenue")
        self.ttm_profit = r.profit_ttm(qe)
        put("ttm_revenue", self.ttm_revenue, qe)
        put("ttm_net_profit", self.ttm_profit, qe)
        put("ttm_eps", r.ttm(qe, "eps_basic"), qe)
        put("npm", f.ratio(self.ttm_profit, self.ttm_revenue), qe)
        if not self.banking:
            ttm_op = f.total(r.op_q(months_before(qe, 3 * n)) for n in range(4))
            put("ttm_operating_profit", ttm_op, qe)
            put("opm", f.ratio(ttm_op, self.ttm_revenue), qe)
            fin = r.ttm(qe, "finance_cost")
            put("interest_coverage", f.positive_ratio(f.ebit(r.ttm(qe, "pbt"), fin), fin), qe)
        year_ago, prev = months_before(qe, 12), months_before(qe, 3)
        put("revenue_yoy", f.growth(r.q(qe, "revenue"), r.q(year_ago, "revenue")), qe)
        put("revenue_qoq", f.growth(r.q(qe, "revenue"), r.q(prev, "revenue")), qe)
        put("profit_yoy", f.growth(r.profit_q(qe), r.profit_q(year_ago)), qe)
        put("profit_qoq", f.growth(r.profit_q(qe), r.profit_q(prev)), qe)
        put("eps_yoy", f.growth(r.q(qe, "eps_basic"), r.q(year_ago, "eps_basic")), qe)

    def years(self, ye: date) -> None:
        r = self.r
        for years in CAGR_YEARS:
            start = months_before(ye, 12 * years)
            sales = f.cagr(r.at(ANNUAL, ye, "revenue"), r.at(ANNUAL, start, "revenue"), years)
            profit = f.cagr(r.profit_annual(ye), r.profit_annual(start), years)
            self.put(f"sales_cagr_{years}y", sales, ye)
            self.put(f"profit_cagr_{years}y", profit, ye)

    def cash_flow(self) -> None:
        r = self.r
        ce = self.book.latest_end(self.basis, ANNUAL, ("cfo",), self.until)
        if ce is None:
            return
        cfo = r.at(ANNUAL, ce, "cfo")
        capex = f.some_total((r.at(ANNUAL, ce, "capex_ppe"), r.at(ANNUAL, ce, "capex_intangibles")))
        fcf = f.free_cash_flow(cfo, capex)
        self.put("cfo", cfo, ce)
        self.put(
            "free_cash_flow", fcf if fcf is not None else r.at(ANNUAL, ce, "free_cash_flow"), ce
        )
        self.put("cfo_to_net_profit", f.ratio(cfo, r.profit_annual(ce)), ce)

    def balances(self, be: date) -> None:
        r, put = self.r, self.put
        prior = months_before(be, 12)
        equity = r.equity(be)
        assets = r.at(INSTANT, be, "total_assets")
        avg_equity = f.average(equity, r.equity(prior))
        avg_assets = f.average(assets, r.at(INSTANT, prior, "total_assets"))
        debt = r.borrowings(be)
        put("equity", equity, be)
        put("borrowings", debt, be)
        put("total_assets", assets, be)
        put("roe", f.positive_ratio(self.ttm_profit, avg_equity), be)
        put("roa", f.positive_ratio(self.ttm_profit, avg_assets), be)
        put("asset_turnover", f.positive_ratio(self.ttm_revenue, avg_assets), be)
        if self.banking:
            return
        put("debt_to_equity", f.positive_ratio(debt, equity), be)
        current = f.ratio(
            r.at(INSTANT, be, "current_assets"), r.at(INSTANT, be, "current_liabilities")
        )
        put("current_ratio", current, be)
        if self.qe is None:
            return
        qe = self.qe
        employed = f.average(
            f.capital_employed(equity, debt),
            f.capital_employed(r.equity(prior), r.borrowings(prior)),
        )
        ebit = f.ebit(r.ttm(qe, "pbt"), r.ttm(qe, "finance_cost"))
        put("roce", f.positive_ratio(ebit, employed), be)
        cogs = r.cogs_ttm(qe)
        put("debtor_days", f.days(r.at(INSTANT, be, "receivables"), self.ttm_revenue), be)
        put("inventory_days", f.days(r.at(INSTANT, be, "inventories"), cogs), be)
        put("payable_days", f.days(r.at(INSTANT, be, "payables"), cogs), be)

    def shares(self) -> tuple[D, date | None]:
        """Shares outstanding: the latest NSE paid-up capital / face value, else the secondary
        source's share capital / face value."""
        r, book = self.r, self.book
        se = book.latest_end(self.basis, QUARTER, ("paid_up_capital",), self.until)
        if se is not None:
            count = f.shares(r.at(QUARTER, se, "paid_up_capital"), r.at(QUARTER, se, "face_value"))
            if count is not None:
                return count, se
        ce = book.latest_end(self.basis, INSTANT, ("share_capital",), self.until)
        fe = book.latest_end(self.basis, INSTANT, ("face_value",), self.until)
        return f.shares(r.at(INSTANT, ce, "share_capital"), r.at(INSTANT, fe, "face_value")), ce

    def valuation(self) -> None:
        r, put, quote = self.r, self.put, self.quote
        count, se = self.shares()
        if quote is not None and count is not None and se is not None:
            after = [factor for ex, factor in quote.splits if se < ex <= quote.day]
            count *= f.split_multiplier(after)
        put("shares", count, se)
        equity = r.equity(self.be)
        put("book_value_per_share", f.positive_ratio(equity, count), self.be)
        cap = f.market_cap(count, quote.close if quote else None)
        anchor = self.qe or self.be or se
        if cap is None or anchor is None or quote is None:
            return
        put("price", quote.close, anchor)
        put("market_cap", cap, anchor)
        put("pe", f.positive_ratio(cap, self.ttm_profit), self.qe)
        put("earnings_yield", f.positive_ratio(self.ttm_profit, cap), self.qe)
        put("pb", f.positive_ratio(cap, equity), self.be)
        ye = self.ye
        if ye is not None:
            paid = r.at(ANNUAL, ye, "dividends_paid")
            payout = r.at(ANNUAL, ye, "dividend_payout")
            amount = f.dividends(paid, payout, r.profit_annual(ye))
            put("dividend_yield", f.positive_ratio(amount, cap), ye)
