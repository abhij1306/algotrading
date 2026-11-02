"""The research panel: per (instrument, month end) the fundamentals a value or quality study reads,
first-published where the store has them and the secondary source's lagged figures otherwise, with
the source of each figure. Read only; written as a Parquet file and a Markdown report under
`$HAT_DATA_ROOT/fundamentals/` (docs/fundamentals.md, Research panel). Nothing in the strategy or
backtest path reads it.

Known when (`research_known`): a first-published filing (NSE XBRL, NSE HTML page, BSE XBRL) from
its `public_at`. A secondary-source page figure from its `public_at` (NSE's first filing of the
period, else the regulatory deadline), not from the fetch as in the point-in-time store: the
figures are restated, which is the price of covering companies and years with no first-published
figure, and each is flagged `screener`. For years ending before FY2020 the cash flow and the
detailed balance sheet were published only in the annual report, so those figures count from the
year end + `ANNUAL_REPORT_DAYS`; the abridged balance sheet (`ABRIDGED`) came with the results.

Per month end, each group of figures (the fiscal year, the TTM, the balance sheet) reads the basis
with its latest figure (`freshest`; consolidated on a tie), named in the row: consolidated
quarterly results were optional until FY2020, so a company's bases change over the years. The
fiscal-year figures are the latest year's; the TTM ones the latest four quarters (`Book.ttm`).
Book value is the first-published year end's when it agrees within `BOOK_TOLERANCE` with the
secondary source's for that year (same basis, else the other), or where that has none with the
year before's plus the year's profit; else the secondary source's, else none: some filers typed
crores into the lakh column of the HTML pages' reserves line.

Shares: first-published paid-up capital / face value, carried to the month end through the
splits and bonuses since (the median of the latest three quarters: a page sometimes has the
capital in the wrong unit); else the secondary source's share capital / today's face value with
each split after that year undone (never EPS). Prices and corporate actions are the
company's across its ISIN changes (`results.universe.successors`).
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...time import IST, as_utc
from . import formulas as f
from .book import (
    CONSOLIDATED,
    SOURCES,
    STANDALONE,
    Book,
    KnownFact,
    Reader,
    month_end,
    months_before,
)
from .items import ANNUAL, INSTANT, QUARTER, YTD

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..store import DataStore

logger = logging.getLogger(__name__)

SCREENER = "screener"
ANNUAL_REPORT_DAYS = 183
ANNUAL_REPORT_UNTIL = date(2020, 3, 31)  # years ending before it: CFO from the annual report
# the abridged balance sheet (statement of assets and liabilities) filed with the annual results
ABRIDGED = frozenset(
    {
        "share_capital",
        "reserves",
        "equity",
        "equity_owners",
        "minority_interest",
        "borrowings",
        "total_assets",
        "total_liabilities",
    }
)
BOOK_TOLERANCE = Decimal("1.5")  # first-published / secondary book value within [1/1.5, 1.5]
STATEMENTS = frozenset({"pl", "bs", "cf", "other"})
END_OF_DAY = time(23, 59)
PANEL = "research-panel.parquet"
BALANCES = ("share_capital", "reserves", "equity", "equity_owners", "borrowings", "total_assets")


def research_known(
    source: str, point_in_time: bool, public_at: datetime, statement: str, item: str, end: date
) -> datetime:
    """When the panel may read one fact (module doc)."""
    if point_in_time or source != SCREENER:
        return as_utc(public_at)
    late = statement == "cf" or (statement == "bs" and item not in ABRIDGED)
    if late and end < ANNUAL_REPORT_UNTIL:
        report = datetime.combine(end + timedelta(days=ANNUAL_REPORT_DAYS), END_OF_DAY, IST)
        return max(as_utc(public_at), as_utc(report))
    return as_utc(public_at)


@dataclass(frozen=True)
class Known:
    """One filing's facts as the panel reads them: one source tag per filing (`SOURCES`; a
    secondary-source page split by when its facts count is two)."""

    at: datetime
    source: str
    basis: str
    period_end: date
    version: int
    facts: tuple[KnownFact, ...]


@dataclass(frozen=True)
class StoredFact:
    statement: str
    period_kind: str
    period_end: date
    item: str
    value: Decimal


def known_filings(
    source: str,
    point_in_time: bool,
    public_at: datetime,
    basis: str,
    period_end: date,
    version: int,
    facts: Iterable[StoredFact],
) -> list[Known]:
    """A stored filing as the panel reads it: its facts grouped by when each counts; a secondary
    page's later group is tagged `screener_late` so it never replaces the earlier one."""
    groups: dict[datetime, list[KnownFact]] = defaultdict(list)
    for x in facts:
        if x.statement not in STATEMENTS:
            continue
        at = research_known(source, point_in_time, public_at, x.statement, x.item, period_end)
        groups[at].append(KnownFact(x.period_kind, x.period_end, x.item, x.value))
    out = []
    for n, at in enumerate(sorted(groups)):
        tag = source if n == 0 else f"{source}_late"
        out.append(Known(at, tag, basis, period_end, version, tuple(groups[at])))
    return out


# --- the shares -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    ex_date: date
    factor: Decimal  # the price factor
    split: bool  # a split (face value changes); else a bonus


def carried(count: Decimal, since: date, day: date, actions: Iterable[Action]) -> Decimal:
    """`count` shares as at `since`, carried to `day` through the splits and bonuses between."""
    return count * f.split_multiplier(a.factor for a in actions if since < a.ex_date <= day)


def screener_shares(
    capital: Decimal | None,
    face_value_now: Decimal | None,
    year_end: date,
    fetched: date,
    actions: Iterable[Action],
) -> Decimal | None:
    """Shares at `year_end` from a secondary-source year: its share capital / the face value at
    the fetch, with each split after the year end (to the fetch) undone (a bonus leaves the face
    value alone, so it needs no undoing)."""
    count = f.shares(capital, face_value_now)
    if count is None:
        return None
    later = [a.factor for a in actions if a.split and year_end < a.ex_date <= fetched]
    return count / f.split_multiplier(later)


# --- one month end --------------------------------------------------------------------------

RANK = {s: n for n, s in enumerate(SOURCES)}


def worst(sources: Iterable[str | None]) -> str | None:
    """The least first-published of `sources` (`SOURCES` order), the flag of a combined figure;
    None when any is missing."""
    found = list(sources)
    if not found or any(s is None for s in found):
        return None
    return max(found, key=lambda s: RANK.get(base(s), len(RANK)))  # type: ignore[arg-type]


def base(source: str | None) -> str:
    return (source or "").removesuffix("_late")


def _source_cells(book: Book, basis: str, kind: str, end: date, items: Sequence[str]) -> str | None:
    found = [book.source(basis, kind, end, i) for i in items]
    return worst([s for s in found if s is not None]) if any(found) else None


def _only(book: Book, basis: str, end: date, item: str, sources: Sequence[str]) -> Decimal | None:
    values = book.cells.get((basis, INSTANT, end, item)) or {}
    return next((values[s] for s in sources if s in values), None)


def freshest(book: Book, kind: str, items: Sequence[str], day: date) -> tuple[str, date] | None:
    """The basis whose latest `kind` figure of `items` is the latest (consolidated on a tie),
    and that period end; None when neither has one."""
    found = [
        (end, b == CONSOLIDATED, b)
        for b in (CONSOLIDATED, STANDALONE)
        if (end := book.latest_end(b, kind, items, day)) is not None
    ]
    if not found:
        return None
    end, _first, basis = max(found)
    return basis, end


def _other(basis: str) -> str:
    return STANDALONE if basis == CONSOLIDATED else CONSOLIDATED


def _screener_value(book: Book, basis: str, end: date) -> Decimal | None:
    """The secondary source's owners' equity at `end`, in `basis` else the other one."""
    for b in (basis, _other(basis)):
        value = _equity_of(book, b, end, (SCREENER, f"{SCREENER}_late"))
        if value is not None:
            return value
    return None


def _equity_of(book: Book, basis: str, end: date, sources: Sequence[str]) -> Decimal | None:
    def at(item: str) -> Decimal | None:
        return _only(book, basis, end, item, sources)

    return f.equity(
        at("equity_owners"),
        at("equity"),
        at("minority_interest"),
        at("share_capital"),
        at("reserves"),
    )


def book_value(book: Book, basis: str, end: date) -> tuple[Decimal | None, str | None]:
    """The year end's owners' equity and its source (module doc): a first-published one checked
    against the secondary source's (same basis, else the other), else, when that has none,
    against the year before's first-published equity plus the year's profit; one failing the
    check gives way to the secondary source's, else to nothing."""
    first = [s for s in SOURCES if base(s) != SCREENER]
    published = _equity_of(book, basis, end, first)
    restated = _screener_value(book, basis, end)
    flag = _source_cells(book, basis, INSTANT, end, ("reserves", "equity", "equity_owners"))
    if published is not None and published > 0:
        check = restated
        if check is None or check <= 0:
            prior = _equity_of(book, basis, months_before(end, 12), first)
            profit = Reader(book, basis).profit_annual(end)
            check = prior + profit if prior is not None and profit is not None else None
        if check is None or check <= 0:
            return published, flag
        if 1 / BOOK_TOLERANCE <= published / check <= BOOK_TOLERANCE:
            return published, flag
    same = _equity_of(book, basis, end, (SCREENER, f"{SCREENER}_late"))
    if same is not None:
        return same, SCREENER
    return (published, flag) if published is not None and published <= 0 else (None, None)


FLOW_ITEMS = ("revenue", "net_profit")


def panel_row(
    book: Book,
    day: date,
    close: tuple[date, Decimal] | None,
    actions: Sequence[Action],
    face_value_now: tuple[date, Decimal] | None,
) -> dict[str, Any] | None:
    """The panel's figures for one instrument at the end of `day` (module doc); None when the
    book has no year and no quarter yet. Each group (the year, the TTM, the balance sheet) reads
    the basis with its latest figure (`freshest`), named in `<group>_basis`."""
    year = freshest(book, ANNUAL, FLOW_ITEMS, day)
    quarter = freshest(book, QUARTER, FLOW_ITEMS, day)
    if year is None and quarter is None:
        return None
    out: dict[str, Any] = {}

    def put(name: str, value: Decimal | None, source: str | None) -> None:
        out[name] = value
        out[f"{name}_src"] = base(source) if value is not None and source else None

    out["fy_basis"], out["fy_end"] = year or (None, None)
    if year is not None:
        basis, ye = year
        r = Reader(book, basis)
        put("revenue", r.at(ANNUAL, ye, "revenue"), book.source(basis, ANNUAL, ye, "revenue"))
        owners = r.at(ANNUAL, ye, "net_profit_owners") is not None
        item = "net_profit_owners" if owners else "net_profit"
        put("net_profit", r.profit_annual(ye), book.source(basis, ANNUAL, ye, item))
        pbt, fin = r.at(ANNUAL, ye, "pbt"), r.at(ANNUAL, ye, "finance_cost")
        put(
            "ebit",
            f.ebit(pbt, fin),
            _source_cells(book, basis, ANNUAL, ye, ("pbt", "finance_cost")),
        )
        dep = r.at(ANNUAL, ye, "depreciation")
        put("depreciation", dep, book.source(basis, ANNUAL, ye, "depreciation"))
        ce = book.latest_end(basis, ANNUAL, ("cfo",), day)
        out["cfo_end"] = ce
        if ce is not None:
            put("cfo", r.at(ANNUAL, ce, "cfo"), book.source(basis, ANNUAL, ce, "cfo"))
    _ttm(book, quarter, out, put)
    balance = freshest(book, INSTANT, BALANCES, day)
    out["bs_basis"], out["bs_end"] = balance or (None, None)
    if balance is not None:
        basis, be = balance
        r = Reader(book, basis)
        equity, source = book_value(book, basis, be)
        put("equity", equity, source)
        debt = ("borrowings", "borrowings_current", "borrowings_noncurrent")
        put("borrowings", r.borrowings(be), _source_cells(book, basis, INSTANT, be, debt))
        assets = r.at(INSTANT, be, "total_assets")
        put("total_assets", assets, book.source(basis, INSTANT, be, "total_assets"))
    preferred = (quarter or year or (CONSOLIDATED, day))[0]
    count, count_src, count_end = shares_at(book, preferred, day, actions, face_value_now)
    put("shares", count, count_src)
    out["shares_end"] = count_end
    if close is not None:
        out["close_day"], out["close"] = close
        out["market_cap"] = count * close[1] if count is not None else None
    return out


Put = Callable[[str, Decimal | None, str | None], None]


def _ttm(book: Book, quarter: tuple[str, date] | None, out: dict[str, Any], put: Put) -> None:
    """The TTM figures: in the latest quarter's basis, else (quarters missing there) the other."""
    out["ttm_basis"] = out["ttm_end"] = None
    if quarter is None:
        return
    first, qe = quarter
    for basis in (first, _other(first)):
        r = Reader(book, basis)
        revenue, profit = r.ttm(qe, "revenue"), r.profit_ttm(qe)
        if revenue is None and profit is None:
            continue
        owners = r.ttm(qe, "net_profit_owners") is not None
        item = "net_profit_owners" if owners else "net_profit"
        out["ttm_basis"], out["ttm_end"] = basis, qe
        put("ttm_revenue", revenue, ttm_source(book, basis, qe, "revenue"))
        put("ttm_net_profit", profit, ttm_source(book, basis, qe, item))
        return


def ttm_source(book: Book, basis: str, qe: date, item: str) -> str | None:
    """The flag of a TTM figure (`Book.ttm`): the worst source of its four quarters (a year-end
    quarter may be the year less the nine months), else the year's when the TTM is the year."""
    found = []
    for n in range(4):
        end = _back(qe, n)
        source = book.source(basis, QUARTER, end, item)
        if source is None:
            parts = [
                book.source(basis, ANNUAL, end, item),
                book.source(basis, YTD, _back(end, 1), item),
            ]
            source = worst(parts)
        found.append(source)
    flag = worst(found)
    return flag if flag is not None else book.source(basis, ANNUAL, qe, item)


def _back(end: date, quarters: int) -> date:
    index = end.year * 12 + end.month - 1 - 3 * quarters
    return month_end(date(index // 12, index % 12 + 1, 1))


SHARE_QUARTERS = 3  # the median of the latest quarters' counts (one typed in the wrong unit)


def shares_at(
    book: Book,
    basis: str,
    day: date,
    actions: Sequence[Action],
    face_value_now: tuple[date, Decimal] | None,
) -> tuple[Decimal | None, str | None, date | None]:
    """Shares outstanding at the end of `day`, its source and the period end it comes from:
    first-published (module doc), the median of the latest `SHARE_QUARTERS` quarters' counts
    each carried to `day`, in `basis` else the other; else the secondary source's."""
    for b in (basis, _other(basis)):
        ends = sorted(book.ends.get((b, QUARTER, "paid_up_capital"), ()), reverse=True)
        counts = []
        for end in [e for e in ends if e <= day][:SHARE_QUARTERS]:
            count = f.shares(book.get(b, QUARTER, end, "paid_up_capital"), _face(book, b, end))
            if count is not None:
                counts.append((carried(count, end, day, actions), end))
        if counts:
            latest = counts[0][1]
            value = sorted(c for c, _e in counts)[(len(counts) - 1) // 2]
            return value, book.source(b, QUARTER, latest, "paid_up_capital"), latest
    if face_value_now is None:
        return None, None, None
    for b in (basis, _other(basis)):
        ce = book.latest_end(b, INSTANT, ("share_capital",), day)
        if ce is None:
            continue
        capital = _only(book, b, ce, "share_capital", [SCREENER, f"{SCREENER}_late"])
        fetched, value = face_value_now
        count = screener_shares(capital, value, ce, fetched, actions)
        if count is not None:
            return carried(count, ce, day, actions), SCREENER, ce
    return None, None, None


def _face(book: Book, basis: str, end: date) -> Decimal | None:
    return book.get(basis, QUARTER, end, "face_value")


def month_ends(start: date, until: date) -> list[date]:
    out = []
    day = month_end(start)
    while day <= until:
        out.append(day)
        day = month_end(day + timedelta(days=1))
    return out


def instrument_panel(
    filings: Sequence[Known],
    days: Sequence[date],
    closes: tuple[list[date], list[Decimal]],
    actions: Sequence[Action],
    face_value_now: tuple[date, Decimal] | None,
) -> list[tuple[date, dict[str, Any]]]:
    """One instrument's panel rows at the end of each of `days` (IST)."""
    ordered = sorted(filings, key=lambda k: (k.at, k.version))
    book = Book()
    out = []
    i = 0
    for day in days:
        moment = as_utc(datetime.combine(day, END_OF_DAY, IST))
        while i < len(ordered) and ordered[i].at <= moment:
            k = ordered[i]
            book.add(k.source, k.basis, k.period_end, k.facts)
            i += 1
        index = bisect_right(closes[0], day)
        close = (closes[0][index - 1], closes[1][index - 1]) if index else None
        row = panel_row(book, day, close, actions, face_value_now)
        if row is not None:
            out.append((day, row))
    return out


# --- coverage and the restatement test ------------------------------------------------------


def fiscal_year(day: date) -> int:
    """The fiscal year (ending March) a day falls in: 2016 for April 2015 to March 2016."""
    return day.year + (day.month > 3)


def coverage(
    members: dict[str, list[tuple[date, date | None]]],
    rows: dict[tuple[str, date], dict[str, Any]],
    days: Sequence[date],
    items: Sequence[str],
) -> dict[int, dict[str, tuple[int, int]]]:
    """Per fiscal year and item: (member month ends with the item, member month ends), a member
    month end being a month end on which the instrument was a NIFTY 500 member."""
    out: dict[int, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for instrument, spans in members.items():
        for day in days:
            if not any(s <= day and (e is None or day < e) for s, e in spans):
                continue
            row = rows.get((instrument, day), {})
            for item in items:
                cell = out[fiscal_year(day)][item]
                cell[1] += 1
                cell[0] += row.get(item) is not None
    return {y: {i: (c[0], c[1]) for i, c in v.items()} for y, v in out.items()}


@dataclass(frozen=True)
class Pair:
    instrument: str
    period_end: date
    basis: str
    item: str
    first: Decimal  # as first published
    restated: Decimal  # the secondary source's


def restatement(pairs: Iterable[Pair]) -> dict[tuple[int, str], dict[str, int]]:
    """Per (fiscal year, item): pairs, how many differ by more than 1%, 5%, 20% (relative to the
    first-published figure), and how many changed sign."""
    out: dict[tuple[int, str], dict[str, int]] = defaultdict(
        lambda: {"n": 0, ">1%": 0, ">5%": 0, ">20%": 0, "sign": 0}
    )
    for p in pairs:
        cell = out[(fiscal_year(p.period_end), p.item)]
        cell["n"] += 1
        if p.first == 0:
            gap = Decimal(0) if p.restated == 0 else Decimal(1)
        else:
            gap = abs(p.restated / p.first - 1)
        for limit, key in (
            (Decimal("0.01"), ">1%"),
            (Decimal("0.05"), ">5%"),
            (Decimal("0.2"), ">20%"),
        ):
            cell[key] += gap > limit
        cell["sign"] += (p.first > 0 > p.restated) or (p.first < 0 < p.restated)
    return dict(out)


# --- the run (database and stores) ----------------------------------------------------------

PANEL_ITEMS = (
    "revenue",
    "net_profit",
    "ebit",
    "cfo",
    "ttm_revenue",
    "ttm_net_profit",
    "equity",
    "borrowings",
    "shares",
    "market_cap",
)
COVERAGE_YEARS = range(2013, 2022)
RESTATEMENT_YEARS = (2016, 2017)
RESTATEMENT_ITEMS = ("revenue", "net_profit", "share_capital", "reserves")


COLUMNS = (
    "fy_basis",
    "fy_end",
    "revenue",
    "net_profit",
    "ebit",
    "depreciation",
    "cfo_end",
    "cfo",
    "ttm_basis",
    "ttm_end",
    "ttm_revenue",
    "ttm_net_profit",
    "bs_basis",
    "bs_end",
    "equity",
    "borrowings",
    "total_assets",
    "shares_end",
    "shares",
    "close_day",
    "close",
    "market_cap",
)
FLAGGED = (
    "revenue",
    "net_profit",
    "ebit",
    "depreciation",
    "cfo",
    "ttm_revenue",
    "ttm_net_profit",
    "equity",
    "borrowings",
    "total_assets",
    "shares",
)


def record(instrument: str, symbol: str, day: date, row: dict[str, Any]) -> dict[str, Any]:
    """One panel row as stored: amounts as floats, every column present, a flag beside each
    flagged figure."""
    out: dict[str, Any] = {"instrument_id": instrument, "symbol": symbol, "as_of": day}
    for name in COLUMNS:
        value = row.get(name)
        out[name] = float(value) if isinstance(value, Decimal) else value
        if name in FLAGGED:
            out[f"{name}_src"] = row.get(f"{name}_src")
    return out


def chain_closes(prices: dict[str, Any], ids: Sequence[str]) -> tuple[list[date], list[Decimal]]:
    """One company's closes across its instruments (`ids`, the latest first): on a day two give,
    the latest instrument's."""
    by_day: dict[date, Decimal] = {}
    for instrument in reversed(ids):
        p = prices.get(instrument)
        if p is not None:
            by_day.update(zip(p.days, p.closes, strict=True))
    days = sorted(by_day)
    return days, [by_day[d] for d in days]


def run(
    store: DataStore, out_dir: Path, since: date, until: date
) -> str:  # pragma: no cover - the database and the stores
    """Build the panel and its report; returns the report's path."""
    import polars as pl

    from app.database import db_session_scope

    from ..results.universe import successors
    from .derive import load_prices

    days = month_ends(since, until)
    covered_days = [d for d in days if fiscal_year(d) in COVERAGE_YEARS]
    records: list[dict[str, Any]] = []
    present: dict[tuple[str, date], dict[str, Any]] = {}
    with db_session_scope() as session:
        filings, symbols = _stored(session)
        actions = _actions(session)
        members = _memberships(session)
        head = successors(session)
        chains: dict[str, list[str]] = {i: [i] for i in filings}
        for old, latest in head.items():
            if latest in chains:
                chains[latest].append(old)
        wanted = {i for ids in chains.values() for i in ids}
        prices = load_prices(store, wanted, since - timedelta(days=40))
        for n, (instrument, stored) in enumerate(filings.items(), 1):
            facts = _facts(session, [s[0] for s in stored])
            known = [k for s in stored for k in known_filings(*s[1:], facts.get(s[0], []))]
            ids = chains[instrument]
            company_actions = sorted(
                {a for i in ids for a in actions.get(i, [])}, key=lambda a: a.ex_date
            )
            closes = chain_closes(prices, ids)
            face = _face_now(stored, facts)
            for day, row in instrument_panel(known, days, closes, company_actions, face):
                records.append(record(instrument, symbols[instrument], day, row))
                if fiscal_year(day) in COVERAGE_YEARS:
                    present[(instrument, day)] = {
                        k: 1 for k in PANEL_ITEMS if row.get(k) is not None
                    }
            if n % 100 == 0:
                logger.info("panel %d/%d instruments, %d rows", n, len(filings), len(records))
        pairs = _pairs(session)
    frame = pl.DataFrame(records, infer_schema_length=None)
    out_dir.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(out_dir / PANEL)
    cov = coverage(members, present, covered_days, PANEL_ITEMS)
    text = report(cov, _source_mix(records), restatement(pairs), len(members), until)
    path = out_dir / f"research-{until}.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _stored(
    session: Session,
) -> tuple[dict[str, list[tuple[Any, ...]]], dict[str, str]]:  # pragma: no cover
    """Every stored filing's keys, by instrument, and each instrument's symbol."""
    from sqlalchemy import select

    from ...models import HatFundamentalFiling

    filing = HatFundamentalFiling
    by: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
    symbols: dict[str, str] = {}
    for row in session.execute(
        select(
            filing.id,
            filing.instrument_id,
            filing.symbol,
            filing.source,
            filing.point_in_time,
            filing.public_at,
            filing.basis,
            filing.period_end,
            filing.version,
        )
    ):
        key = str(row.instrument_id)
        symbols[key] = row.symbol
        by[key].append(
            (
                str(row.id),
                row.source,
                row.point_in_time,
                row.public_at,
                row.basis,
                row.period_end,
                row.version,
            )
        )
    return by, symbols


def _facts(
    session: Session, filing_ids: Sequence[str]
) -> dict[str, list[StoredFact]]:  # pragma: no cover
    """The canonical facts of these filings, by filing."""
    import uuid

    from sqlalchemy import select

    from ...models import HatFundamentalFact

    x = HatFundamentalFact
    facts: dict[str, list[StoredFact]] = defaultdict(list)
    for filing_id, statement, kind, end, item, value in session.execute(
        select(x.filing_id, x.statement, x.period_kind, x.period_end, x.item, x.value).where(
            x.filing_id.in_([uuid.UUID(i) for i in filing_ids]), x.item.is_not(None)
        )
    ):
        facts[str(filing_id)].append(StoredFact(statement, kind, end, item, value))
    return facts


def _face_now(
    stored: Sequence[tuple[Any, ...]], facts: dict[str, list[StoredFact]]
) -> tuple[date, Decimal] | None:
    """The face value the secondary source's page showed when fetched (its snapshot)."""
    best: tuple[date, Decimal] | None = None
    for filing_id, source, *_rest in stored:
        if source != SCREENER:
            continue
        for x in facts.get(filing_id, []):
            if x.statement == "snapshot" and x.item == "face_value":
                if best is None or x.period_end > best[0]:
                    best = (x.period_end, x.value)
    return best


def _actions(session: Session) -> dict[str, list[Action]]:  # pragma: no cover - the database
    from sqlalchemy import select

    from ...models import HatCorporateAction

    out: dict[str, list[Action]] = defaultdict(list)
    for instrument_id, ex_date, factor, kind in session.execute(
        select(
            HatCorporateAction.instrument_id,
            HatCorporateAction.ex_date,
            HatCorporateAction.adjustment_factor,
            HatCorporateAction.action_type,
        ).where(HatCorporateAction.action_type.in_(("split", "bonus")))
    ):
        out[str(instrument_id)].append(Action(ex_date, Decimal(factor), kind == "split"))
    return {k: sorted(set(v), key=lambda a: a.ex_date) for k, v in out.items()}


def _memberships(session: Session) -> dict[str, list[tuple[date, date | None]]]:  # pragma: no cover
    """Each company's NIFTY 500 membership spans, keyed by its latest instrument (the panel's)."""
    from sqlalchemy import select

    from ...models import HatIndexMember
    from ..results.universe import NIFTY_500, successors

    head = successors(session)
    out: dict[str, list[tuple[date, date | None]]] = defaultdict(list)
    for instrument_id, start, end in session.execute(
        select(
            HatIndexMember.instrument_id, HatIndexMember.effective_from, HatIndexMember.effective_to
        ).where(HatIndexMember.index_name == NIFTY_500)
    ):
        key = str(instrument_id)
        out[head.get(key, key)].append((start, end))
    return dict(out)


_PAIRS_SQL = """
WITH first_html AS (
  SELECT DISTINCT ON (instrument_id, basis, period_end) id, instrument_id, basis, period_end
  FROM hat_fundamental_filing WHERE source = 'nse_html' AND extract(month FROM period_end) = 3
  ORDER BY instrument_id, basis, period_end, version
)
SELECT h.instrument_id, h.period_end, h.basis, x.item, x.value, s.value
FROM first_html h
JOIN hat_fundamental_fact x ON x.filing_id = h.id
  AND x.item = ANY(:items) AND x.period_kind IN ('annual', 'instant')
JOIN hat_fundamental_filing sf ON sf.instrument_id = h.instrument_id AND sf.source = 'screener'
  AND sf.basis = h.basis AND sf.period_end = h.period_end
JOIN hat_fundamental_fact s ON s.filing_id = sf.id AND s.item = x.item
  AND s.period_kind = x.period_kind AND s.period_end = x.period_end
WHERE extract(year FROM h.period_end) = ANY(:years)
"""


def _pairs(session: Session) -> list[Pair]:  # pragma: no cover - the database
    from sqlalchemy import text

    from ...models import HatFundamentalFiling

    rows = session.execute(
        text(_PAIRS_SQL),
        {"items": list(RESTATEMENT_ITEMS), "years": list(RESTATEMENT_YEARS)},
        bind_arguments={"mapper": HatFundamentalFiling},
    )
    return [
        Pair(str(i), end, basis, item, first, restated)
        for i, end, basis, item, first, restated in rows
    ]


def _source_mix(records: Sequence[dict[str, Any]]) -> dict[int, dict[str, dict[str, int]]]:
    out: dict[int, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )
    for r in records:
        year = fiscal_year(r["as_of"])
        if year not in COVERAGE_YEARS:
            continue
        for item in ("revenue", "net_profit", "ttm_net_profit", "equity", "cfo", "shares"):
            source = r.get(f"{item}_src")
            if source:
                out[year][item][source] += 1
    return out


def report(
    cov: dict[int, dict[str, tuple[int, int]]],
    sources: dict[int, dict[str, dict[str, int]]],
    restated: dict[tuple[int, str], dict[str, int]],
    companies: int,
    until: date,
) -> str:
    """The panel's report as Markdown."""
    lines = [
        f"# Fundamentals research panel, {until}",
        "",
        f"NIFTY 500 point-in-time companies with a membership loaded: {companies}.",
        "Coverage is the share of member month ends (a month end on which the company was a",
        "member) whose panel row has the figure, per fiscal year (April to March, named by its",
        "end).",
        "",
        "## Member coverage (%)",
        "",
        "| item | " + " | ".join(f"FY{y}" for y in sorted(cov)) + " |",
        "|---|" + "---|" * len(cov),
    ]
    for item in PANEL_ITEMS:
        cells = []
        for year in sorted(cov):
            hit, total = cov[year].get(item, (0, 0))
            cells.append(f"{100 * hit / total:.1f}" if total else "-")
        lines.append(f"| {item} | " + " | ".join(cells) + " |")
    totals = [cov[y].get("revenue", (0, 0))[1] for y in sorted(cov)]
    lines.append("| member month ends | " + " | ".join(str(t) for t in totals) + " |")
    lines += ["", "## Sources of the figures (% of rows with the figure)", ""]
    for item in ("revenue", "net_profit", "ttm_net_profit", "equity", "cfo", "shares"):
        lines += [
            f"### {item}",
            "",
            "| FY | " + " | ".join(SOURCES) + " |",
            "|---|" + "---|" * len(SOURCES),
        ]
        for year in sorted(sources):
            counts = sources[year].get(item, {})
            total = sum(counts.values()) or 1
            lines.append(
                f"| {year} | "
                + " | ".join(f"{100 * counts.get(s, 0) / total:.1f}" for s in SOURCES)
                + " |"
            )
        lines.append("")
    lines += [
        "## Restatement test: NSE first-published (HTML annual page) vs the secondary source",
        "",
        "Same company, basis and year end; the share of pairs whose secondary-source figure",
        "differs from the first-published one by more than each threshold (relative), and sign",
        "changes.",
        "",
        "| FY | item | n | >1% | >5% | >20% | sign flip |",
        "|---|---|---|---|---|---|---|",
    ]
    for (year, item), c in sorted(restated.items()):
        n = c["n"] or 1
        lines.append(
            f"| {year} | {item} | {c['n']} | {100 * c['>1%'] / n:.1f} | {100 * c['>5%'] / n:.1f} | "
            f"{100 * c['>20%'] / n:.1f} | {100 * c['sign'] / n:.1f} |"
        )
    return "\n".join(lines) + "\n"
