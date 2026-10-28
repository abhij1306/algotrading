"""The coverage report of the fundamentals store, as Markdown: per source, statement and year how
many instruments have each canonical item; the depth of the secondary source's annual history;
NSE and secondary-source agreement on the quarters both give; the gaps; and one company's full
sheet laid out like the secondary source's page. Reads the store only.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from . import formulas as f
from .book import Book, Reader, months_before
from .items import ANNUAL, INSTANT, QUARTER

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

CRORE = Decimal(10) ** 7
Run = Callable[..., list[tuple]]
AGREE_ITEMS = ("revenue", "pbt", "net_profit", "eps_basic", "finance_cost", "depreciation")
BANDS = (Decimal("0.005"), Decimal("0.02"), Decimal("0.10"))
SHEET_QUARTERS = 13
SHEET_ROWS = {
    "quarters": (
        "revenue",
        "operating_profit",
        "other_income",
        "finance_cost",
        "depreciation",
        "pbt",
        "tax",
        "net_profit",
        "net_profit_owners",
        "eps_basic",
    ),
    "profit-loss": (
        "revenue",
        "total_expenses",
        "operating_profit",
        "other_income",
        "finance_cost",
        "depreciation",
        "pbt",
        "tax",
        "net_profit",
        "net_profit_owners",
        "eps_basic",
        "dividend_payout",
    ),
    "balance-sheet": (
        "share_capital",
        "reserves",
        "equity_owners",
        "borrowings",
        "borrowings_noncurrent",
        "borrowings_current",
        "total_liabilities",
        "fixed_assets",
        "cwip",
        "investments",
        "inventories",
        "receivables",
        "cash",
        "current_assets",
        "current_liabilities",
        "total_assets",
    ),
    "cash-flow": ("cfo", "cfi", "cff", "net_cash_flow", "capex_ppe", "free_cash_flow"),
    "ratios": ("debtor_days", "inventory_days", "payable_days", "working_capital_days", "roce"),
}
SHEET_KIND = {
    "quarters": QUARTER,
    "profit-loss": ANNUAL,
    "balance-sheet": INSTANT,
    "cash-flow": ANNUAL,
    "ratios": ANNUAL,
}
UNSCALED = {"eps_basic", "debtor_days", "inventory_days", "payable_days", "working_capital_days"}
MONEY_METRICS = {
    "ttm_revenue",
    "ttm_net_profit",
    "ttm_operating_profit",
    "equity",
    "borrowings",
    "total_assets",
    "cfo",
    "free_cash_flow",
    "market_cap",
}
FRACTIONS = {"dividend_payout", "roce", "opm", "tax_rate"}


def pairs(rows: Iterable[tuple]) -> list[tuple[str, Decimal, Decimal]]:
    """(item, NSE value, secondary value) for each (instrument, basis, quarter, item) both
    sources give, from rows of (instrument, basis, period end, item, source, value)."""
    found: dict[tuple, dict[str, Decimal]] = defaultdict(dict)
    for instrument, basis, end, item, source, value in rows:
        found[(instrument, basis, end, item)][source] = value
    return [
        (key[3], by["nse"], by["screener"])
        for key, by in found.items()
        if "nse" in by and "screener" in by
    ]


def band(nse: Decimal, other: Decimal) -> str:
    """How far the secondary source's figure is from NSE's, relative to NSE's."""
    if nse == other:
        return "equal"
    if nse == 0:
        return "> 10%"
    diff = abs(other - nse) / abs(nse)
    for limit, name in zip(BANDS, ("<= 0.5%", "<= 2%", "<= 10%"), strict=True):
        if diff <= limit:
            return name
    return "> 10%"


def table(header: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def shown(item: str, value: Decimal | None) -> str:
    """A sheet cell: crore rupees with no decimals (EPS and days as they are, a fraction as a
    percentage), blank when not known."""
    if value is None:
        return ""
    if item in FRACTIONS:
        return f"{value * 100:.1f}%"
    if item in UNSCALED:
        return f"{value:.2f}"
    return f"{value / CRORE:,.0f}"


def metric_shown(name: str, value: Decimal) -> str:
    """A derived metric as the sheet shows it: money in crore rupees, shares in crore, the rest
    as stored."""
    if name in MONEY_METRICS:
        return f"{value / CRORE:,.0f} cr"
    if name == "shares":
        return f"{value / CRORE:,.2f} cr shares"
    return f"{value:,.4f}"


def sheet(book: Book, basis: str, until: date) -> str:
    """One company's figures known in `book`, by section (NSE's where both sources give one)."""
    parts = []
    for section, items in SHEET_ROWS.items():
        kind = SHEET_KIND[section]
        if section == "quarters":
            last = book.latest_end(basis, QUARTER, ("revenue",), until)
            if last is None:
                continue
            ends = [months_before(last, 3 * n) for n in reversed(range(SHEET_QUARTERS))]
        else:
            ends = sorted(
                {e for item in items for e in book.ends.get((basis, kind, item), ()) if e <= until}
            )
            ends = [e for e in ends if e.month == 3 or kind != INSTANT][-12:]
        rows = []
        for item in items:
            values = [_cell(book, basis, kind, end, item) for end in ends]
            if any(v is not None for v in values):
                rows.append([item, *(shown(item, v) for v in values)])
        if rows:
            header = [section, *(e.strftime("%b %Y") for e in ends)]
            parts.append(f"#### {section}\n\n" + table(header, rows))
    return "\n\n".join(parts)


def _cell(book: Book, basis: str, kind: str, end: date, item: str) -> Decimal | None:
    if kind == QUARTER:
        if item == "operating_profit":
            return Reader(book, basis).op_q(end)
        return book.quarter(basis, end, item)
    if item == "operating_profit":
        stated = f.operating_profit(
            book.get(basis, kind, end, "revenue"),
            book.get(basis, kind, end, "total_expenses"),
            book.get(basis, kind, end, "finance_cost"),
            book.get(basis, kind, end, "depreciation"),
        )
        return stated if stated is not None else book.get(basis, kind, end, item)
    return book.get(basis, kind, end, item)


# --- the report -------------------------------------------------------------------------------

_COVERAGE_SQL = """
SELECT f.source, x.statement, x.item, extract(year FROM x.period_end)::int AS year,
       count(DISTINCT f.instrument_id)
FROM hat_fundamental_fact x JOIN hat_fundamental_filing f ON f.id = x.filing_id
WHERE x.item IS NOT NULL
GROUP BY 1, 2, 3, 4
"""

_AGREEMENT_SQL = """
WITH latest AS (
  SELECT DISTINCT ON (instrument_id, source, basis, period_end) id, instrument_id, source, basis
  FROM hat_fundamental_filing ORDER BY instrument_id, source, basis, period_end, version DESC
)
SELECT l.instrument_id, l.basis, x.period_end, x.item, l.source, x.value
FROM hat_fundamental_fact x JOIN latest l ON l.id = x.filing_id
WHERE x.item = ANY(:items) AND x.period_kind = 'quarter'
"""

_DEPTH_SQL = """
SELECT f.instrument_id, count(DISTINCT x.period_end)
FROM hat_fundamental_fact x JOIN hat_fundamental_filing f ON f.id = x.filing_id
WHERE f.source = 'screener' AND x.context = 'profit-loss' AND x.item = 'revenue'
GROUP BY 1
"""

_FAMILY_SQL = """
SELECT f.family, f.source, count(*), count(DISTINCT f.instrument_id),
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM hat_fundamental_fact x
         WHERE x.filing_id = f.id AND x.period_kind = 'instant' AND x.statement = 'bs'
           AND x.item IS NOT NULL)),
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM hat_fundamental_fact x
         WHERE x.filing_id = f.id AND x.statement = 'cf' AND x.item IS NOT NULL))
FROM hat_fundamental_filing f GROUP BY 1, 2 ORDER BY 2, 1
"""

_HALF_YEAR_SQL = """
SELECT extract(year FROM f.period_end)::int, extract(month FROM f.period_end)::int,
       count(DISTINCT f.instrument_id),
       count(DISTINCT f.instrument_id) FILTER (WHERE EXISTS (SELECT 1 FROM hat_fundamental_fact x
         WHERE x.filing_id = f.id AND x.item = 'total_assets' AND x.period_kind = 'instant')),
       count(DISTINCT f.instrument_id) FILTER (WHERE EXISTS (SELECT 1 FROM hat_fundamental_fact x
         WHERE x.filing_id = f.id AND x.item = 'cfo'))
FROM hat_fundamental_filing f
WHERE f.source = 'nse' AND f.family <> 'insurer' AND extract(month FROM f.period_end) IN (3, 9)
GROUP BY 1, 2 ORDER BY 1, 2
"""


def build(
    session: Session, example: str, members: int, outcomes: dict[str, int] | None
) -> str:  # pragma: no cover - the database
    from sqlalchemy import text

    from ...models import HatFundamentalFiling
    from ...time import utc_now

    on = {"mapper": HatFundamentalFiling}

    def run(sql: str, **params: object) -> list[tuple]:
        return [tuple(r) for r in session.execute(text(sql), params, bind_arguments=on)]

    now = utc_now()
    out = [
        f"# Fundamentals coverage, {now.date()}",
        "",
        "Generated by `python -m app.hat.data.fundamentals report` from `hat_fundamental_filing`,"
        " `hat_fundamental_fact` and `hat_fundamental_metric` (docs/fundamentals.md). Counts are"
        f" instruments; {members} instruments are covered (NIFTY 500 point-in-time members from"
        " 2021). Secondary-source (screener.in) figures are as restated when the pages were"
        " fetched, not the point-in-time originals.",
    ]
    out += _sources(run, outcomes)
    out += _coverage(run)
    out += _depth(run)
    out += _agreement(run)
    out += _gaps(run)
    out += _example(session, example, now)
    return "\n".join(out) + "\n"


def _sources(run: Run, outcomes: dict[str, int] | None) -> list[str]:
    rows = run(
        "SELECT source, count(*), count(DISTINCT instrument_id), sum(facts), min(period_end),"
        " max(period_end) FROM hat_fundamental_filing GROUP BY 1 ORDER BY 1"
    )
    out = ["", "## Sources", ""]
    out.append(
        table(("source", "filings", "instruments", "facts", "first period", "last period"), rows)
    )
    if outcomes:
        out += ["", "Extract outcomes (NSE documents and pages):", ""]
        out.append(table(("outcome", "count"), sorted(outcomes.items())))
    return out


def _coverage(run: Run) -> list[str]:
    found: dict[tuple[str, str], dict[str, dict[int, int]]] = defaultdict(lambda: defaultdict(dict))
    years: set[int] = set()
    for source, statement, item, year, count in run(_COVERAGE_SQL):
        found[(source, statement)][item][year] = count
        years.add(year)
    out = ["", "## Coverage: instruments with each item, by period-end year", ""]
    for (source, statement), items in sorted(found.items()):
        span = sorted({y for counts in items.values() for y in counts})
        out += [f"### {source} / {statement}", ""]
        rows = [
            [item, *(counts.get(y, "") for y in span)] for item, counts in sorted(items.items())
        ]
        out += [table(("item", *map(str, span)), rows), ""]
    return out


def _depth(run: Run) -> list[str]:
    depths = sorted(count for _i, count in run(_DEPTH_SQL))
    out = ["", "## Secondary-source annual history depth", ""]
    if not depths:
        return [*out, "No secondary-source annual figures."]
    histogram = Counter(depths)
    out.append(
        f"{len(depths)} instruments have annual profit-and-loss figures; years each: min"
        f" {depths[0]}, median {statistics.median(depths)}, max {depths[-1]}."
    )
    out += ["", table(("years", "instruments"), sorted(histogram.items()))]
    return out


def _agreement(run: Run) -> list[str]:
    by_item: dict[str, Counter[str]] = defaultdict(Counter)
    diffs: dict[str, list[Decimal]] = defaultdict(list)
    for item, nse, other in pairs(run(_AGREEMENT_SQL, items=list(AGREE_ITEMS))):
        by_item[item][band(nse, other)] += 1
        if nse:
            diffs[item].append(abs(other - nse) / abs(nse))
    names = ("equal", "<= 0.5%", "<= 2%", "<= 10%", "> 10%")
    rows = []
    for item in AGREE_ITEMS:
        counts = by_item.get(item, Counter())
        middle = statistics.median(diffs[item]) if diffs[item] else None
        rows.append(
            [
                item,
                sum(counts.values()),
                *(counts.get(n, 0) for n in names),
                "" if middle is None else f"{middle * 100:.2f}%",
            ]
        )
    return [
        "",
        "## NSE and secondary-source agreement (same instrument, basis and quarter)",
        "",
        "The secondary source rounds to a crore, so small figures differ by rounding alone; its"
        " net profit is the period's profit (owners' and non-controlling), compared with NSE's"
        " `net_profit` (the period's profit too). NSE wins wherever both exist.",
        "",
        table(("item", "compared", *names, "median difference"), rows),
    ]


def _gaps(run: Run) -> list[str]:
    out = ["", "## Gaps", "", "### Filings by taxonomy family", ""]
    out.append(
        table(
            (
                "family",
                "source",
                "filings",
                "instruments",
                "with a mapped balance sheet",
                "with a mapped cash flow",
            ),
            run(_FAMILY_SQL),
        )
    )
    out += [
        "",
        "Banks: no current assets/liabilities, inventories or receivables (their balance sheet"
        " is deposits, advances and investments), so the working-capital ratios, debt/equity,"
        " ROCE and interest coverage are not derived for them; `total_expenses` is before"
        " provisions. NBFCs: borrowings are debt securities + borrowings + subordinated"
        " liabilities; no current/non-current split. Insurers: the insurance taxonomies"
        " share almost no elements with Ind AS; their facts are stored with raw elements"
        " only.",
        "",
        "### Half-year balance sheet and cash flow in NSE filings (March and September)",
        "",
        table(
            ("year", "month", "instruments", "with total assets", "with CFO"),
            run(_HALF_YEAR_SQL),
        ),
    ]
    return out


def _example(session: Session, symbol: str, now: datetime) -> list[str]:  # pragma: no cover
    from sqlalchemy import select

    from ...models import HatFundamentalFact, HatFundamentalFiling
    from .book import choose_basis
    from .derive import StoredFiling, book_as_of, metrics_as_of, stored_facts

    rows = list(
        session.scalars(select(HatFundamentalFiling).where(HatFundamentalFiling.symbol == symbol))
    )
    if not rows:
        return ["", f"## Example: {symbol}", "", "Not in the store."]
    filings = [StoredFiling.of(r) for r in rows]
    book = book_as_of(
        filings, stored_facts(session, HatFundamentalFact, [f.id for f in filings]), now
    )
    basis = choose_basis(book, now.date())
    found = metrics_as_of(session, rows[0].instrument_id, now)
    ratio_rows = [
        [name, metric_shown(name, value), end] for name, (value, end) in sorted(found.items())
    ]
    return [
        "",
        f"## Example: {symbol} ({basis}), as known at {now:%Y-%m-%d %H:%M} UTC",
        "",
        "Rupees crore (EPS and days as they are). NSE's figure where both sources give one.",
        "",
        sheet(book, basis, now.date()),
        "",
        "#### derived metrics (hat_fundamental_metric, latest)",
        "",
        table(("metric", "value", "period end"), ratio_rows),
    ]
