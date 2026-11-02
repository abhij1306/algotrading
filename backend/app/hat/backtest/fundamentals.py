"""A rotation's point-in-time fundamentals screen (`strategy.config.Fundamentals`).

The figures are the research panel's (`data/fundamentals/research.py`): first-published before
restated, a secondary-source (restated) figure only from its own public date (`research_known`).
`load_market` reads them once for the run's members (`load`), as a `History` per member: the
figures after each date a filing became public (IST), replayed in order (`history`), so a later
version of a filing replaces its predecessor only from its own public date. Nothing after the
period's end is read.

On a decision close, a figure public on day P is usable from the close of the session
`lag_sessions` sessions after P (the session on or after P itself with 0): the screen reads the
latest figures public by `cutoff`, and a figure whose period ended more than `max_age_days`
before the decision close counts as missing (`aged`). Valuation is priced on the decision close:
the share count is held on the run's price basis (`basis_shares`: the count over the price
basis's factor on the day the figures were taken), so a split or bonus since is in the close it
is multiplied by.

The figures (`figures`), floats, all in one basis per snapshot (the basis of the latest
quarter's profit and loss, else the latest year's, the panel's `freshest`, consolidated on a
tie): a figure that basis lacks is missing, never taken from the other.
- `roce`: EBIT / (equity + borrowings) at the basis's latest balance sheet; EBIT the TTM's
  (profit before tax + finance cost), else the latest year's when that is newer or the TTM is
  incomplete; -inf when that capital is not positive.
- `gearing`: borrowings / equity there; inf when equity is not positive.
- `cfo`: the basis's latest year's cash flow from operations.
- `profit`: the TTM owners' profit, else the latest year's (as EBIT).
- `ebit`, `net_debt`: as above; borrowings less cash at the latest balance sheet.
- `family`: the taxonomy family of the latest first-published filing known then (a company that
  converts from an NBFC to a bank changes family at its first bank filing).

A name is a financial (`kind`) when that family is a bank's, an NBFC's or an insurer's, or its
industry on the trade date is `FINANCIAL_INDUSTRY`; it is an ordinary company when the family is
the corporate Ind AS one or it has another known industry; otherwise its kind is unknown (an HTML
page before 2018 does not tell an NBFC from a company), and then the checks only an ordinary
company has count as missing: a bank is never screened as an ordinary company by default.

The screen (`screen`) on the eligible names, in order: `quality` drops a name failing a check
(`ROCE`, `GEARING`, `CFO`, the first failing in that order). ROCE and debt/equity do not apply to
a financial: they pass. A name lacking a figure a check needs and failing none is kept or dropped
by `missing` (`MISSING_KEPT`, `MISSING_DROPPED`). Then `value` ranks the remaining names with the
multiple (`earnings_yield`: profit / market cap; `ebit_ev`: EBIT / enterprise value, market cap
+ net debt, +inf for a non-positive value with a positive EBIT, else -inf) and drops the
cheapest-yielding `drop_pct` percent of them, rounded down (`VALUE`): a loss yields below 0, so
it is the most expensive. Under `ebit_ev` the financials are ranked apart, on earnings yield,
and lose the same share. A name without its multiple is not ranked and is kept or dropped by
`missing`, counted once a night. A financial's reasons end in `_financial`, and one with no
other mark is marked kept as `FINANCIAL`, so the run counts them apart.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
import polars as pl

from ..data.fundamentals import formulas as f
from ..data.fundamentals.book import Book, Reader
from ..data.fundamentals.items import ANNUAL, INSTANT, QUARTER
from ..data.fundamentals.research import (
    BALANCES,
    FLOW_ITEMS,
    SCREENER,
    Action,
    Company,
    Known,
    base,
    book_value,
    freshest,
    shares_at,
)
from ..time import IST

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = [
    "CFO",
    "FINANCIAL",
    "FINANCIAL_INDUSTRY",
    "GEARING",
    "MISSING_DROPPED",
    "MISSING_KEPT",
    "ROCE",
    "VALUE",
    "Figures",
    "History",
    "Mark",
    "Screen",
    "aged",
    "basis_shares",
    "cutoff",
    "digest_text",
    "factor_on",
    "figures",
    "history",
    "kind",
    "load",
    "multiple",
    "screen",
    "snapshot",
]

# skips.csv reasons (`loop._fundamental_screen`); a financial's end in `_financial`
ROCE = "fundamentals_roce"
GEARING = "fundamentals_gearing"
CFO = "fundamentals_cfo"
VALUE = "fundamentals_value"
MISSING_KEPT = "fundamentals_missing_kept"
MISSING_DROPPED = "fundamentals_missing_dropped"
FINANCIAL = "fundamentals_financial"  # a financial kept with no other mark
_FINANCIAL_SUFFIX = "_financial"

FINANCIAL_FAMILIES = frozenset({"bank", "nbfc", "insurer"})
CORPORATE_FAMILY = "indas"
# the NIFTY 500 list's industry of banks, NBFCs, insurers and the other financials
FINANCIAL_INDUSTRY = "Financial Services"
NO_INDUSTRY = "unknown"  # `data.industry_on` without a classification
ORDINARY, FINANCIAL_KIND, UNKNOWN = "ordinary", "financial", "unknown"


class FundamentalQuality(Protocol):
    """`strategy.config.FundamentalQuality`, as the screen reads it."""

    @property
    def min_roce(self) -> float | None: ...
    @property
    def max_gearing(self) -> float | None: ...
    @property
    def positive_cfo(self) -> bool: ...


class FundamentalValue(Protocol):
    """`strategy.config.FundamentalValue`, as the screen reads it."""

    @property
    def multiple(self) -> str: ...
    @property
    def drop_pct(self) -> float: ...


class Fundamentals(Protocol):
    """`strategy.config.Fundamentals`, as the screen reads it."""

    @property
    def quality(self) -> FundamentalQuality | None: ...
    @property
    def value(self) -> FundamentalValue | None: ...
    @property
    def missing(self) -> str: ...


@dataclass(frozen=True)
class Figures:
    """A name's figures as known on one day (module doc); None where unknown. `flow_end`,
    `balance_end` and `cfo_end` are the period ends the flows (profit, EBIT), the balance sheet
    and the cash flow come from (`aged`)."""

    roce: float | None = None
    gearing: float | None = None
    cfo: float | None = None
    profit: float | None = None
    ebit: float | None = None
    net_debt: float | None = None
    shares: float | None = None  # on the run's price basis (`basis_shares`)
    family: str | None = None
    flow_end: date | None = None
    balance_end: date | None = None
    cfo_end: date | None = None


@dataclass(frozen=True)
class History:
    """A name's figures after each day a filing became public (IST), ascending."""

    days: tuple[date, ...]
    figures: tuple[Figures, ...]

    def at(self, day: date | None) -> Figures | None:
        """The figures public by the end of `day`; None before the first."""
        if day is None:
            return None
        n = bisect_right(self.days, day)
        return self.figures[n - 1] if n else None


def _float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def basis_shares(count: Decimal | None, factor: float | None) -> float | None:
    """A share count on `day` on the run's price basis: the count over the basis's factor that
    day (the adjusted close over the raw one), so that times a later adjusted close it is the
    market cap then, whatever split or bonus came between."""
    if count is None or factor is None or not factor > 0:
        return None
    return float(count) / factor


def _flows(
    r: Reader, qe: date | None, ye: date | None
) -> tuple[Decimal | None, Decimal | None, date | None]:
    """(EBIT, profit, their period end) in `r`'s basis: the TTM to `qe`, else the year `ye`
    when the TTM is incomplete or the year is newer."""
    if qe is not None and (ye is None or qe >= ye):
        ebit = f.ebit(r.ttm(qe, "pbt"), r.ttm(qe, "finance_cost"))
        profit = r.profit_ttm(qe)
        if ebit is not None or profit is not None:
            return ebit, profit, qe
    if ye is None:
        return None, None, None
    ebit = f.ebit(r.at(ANNUAL, ye, "pbt"), r.at(ANNUAL, ye, "finance_cost"))
    return ebit, r.profit_annual(ye), ye


def figures(
    book: Book,
    day: date,
    actions: Sequence[Action],
    face_value_now: tuple[date, Decimal] | None,
    *,
    family: str | None,
    factor: float | None,
) -> Figures:
    """The figures of `book` at the end of `day` (module doc), in one basis."""
    chosen = freshest(book, QUARTER, FLOW_ITEMS, day) or freshest(book, ANNUAL, FLOW_ITEMS, day)
    if chosen is None:
        return Figures(family=family)
    basis = chosen[0]
    r = Reader(book, basis)
    qe = book.latest_end(basis, QUARTER, FLOW_ITEMS, day)
    ye = book.latest_end(basis, ANNUAL, FLOW_ITEMS, day)
    ebit, profit, flow_end = _flows(r, qe, ye)
    ce = book.latest_end(basis, ANNUAL, ("cfo",), day)
    cfo = r.at(ANNUAL, ce, "cfo")
    count = shares_at(book, basis, day, actions, face_value_now)[0]
    be = book.latest_end(basis, INSTANT, BALANCES, day)
    equity = debt = cash = None
    if be is not None:
        equity = book_value(book, basis, be)[0]
        debt = r.borrowings(be)
        cash = r.at(INSTANT, be, "cash")
    employed = f.capital_employed(equity, debt)
    roce = None
    if employed is not None and ebit is not None:
        roce = float(ebit / employed) if employed > 0 else -math.inf
    gearing = None
    if equity is not None and equity <= 0:
        gearing = math.inf
    elif equity is not None and debt is not None:
        gearing = float(debt / equity)
    return Figures(
        roce=roce,
        gearing=gearing,
        cfo=_float(cfo),
        profit=_float(profit),
        ebit=_float(ebit),
        net_debt=None if debt is None or cash is None else float(debt - cash),
        shares=basis_shares(count, factor),
        family=family,
        flow_end=flow_end,
        balance_end=be,
        cfo_end=ce if cfo is not None else None,
    )


def history(company: Company, factor: Callable[[date], float | None], until: date) -> History:
    """`company`'s figures after each day one of its filings became public, through `until`;
    `factor` the price basis's factor on a day (`basis_shares`). The family is the latest
    first-published filing's known by that day."""
    ordered = sorted(company.known, key=lambda k: (k.at, k.version))
    by_day: dict[date, list[Known]] = {}
    for k in ordered:
        day = k.at.astimezone(IST).date()
        if day <= until:
            by_day.setdefault(day, []).append(k)
    book = Book()
    family: str | None = None
    days: list[date] = []
    out: list[Figures] = []
    for day, filings in by_day.items():
        for k in filings:
            book.add(k.source, k.basis, k.period_end, k.facts)
            if base(k.source) != SCREENER and k.family:
                family = k.family
        found = figures(
            book,
            day,
            company.actions,
            company.face_value_now,
            family=family,
            factor=factor(day),
        )
        if not out or found != out[-1]:
            days.append(day)
            out.append(found)
    return History(tuple(days), tuple(out))


def aged(figs: Figures | None, day: date, max_days: int) -> Figures | None:
    """`figs` read on `day`, each figure whose period ended more than `max_days` before it
    missing (ROCE when either of its parts is)."""
    if figs is None:
        return None

    def stale(end: date | None) -> bool:
        return end is None or (day - end).days > max_days

    out = figs
    if stale(figs.flow_end):
        out = replace(out, profit=None, ebit=None, roce=None)
    if stale(figs.balance_end):
        out = replace(out, gearing=None, net_debt=None, roce=None)
    if stale(figs.cfo_end):
        out = replace(out, cfo=None)
    return out


def cutoff(days: Sequence[date], at: int, lag: int) -> date | None:
    """The last public date a decision on the close of `days[at]` may read under a lag of `lag`
    sessions: `days[at - lag]` (a figure public on day P is read from the close of the
    session `lag` sessions after the first one on or after P); None before the first."""
    return days[at - lag] if at - lag >= 0 else None


def kind(family: str | None, industry: str | None) -> str:
    """A name's kind (module doc): financial, ordinary or unknown."""
    if family in FINANCIAL_FAMILIES or industry == FINANCIAL_INDUSTRY:
        return FINANCIAL_KIND
    if family == CORPORATE_FAMILY or industry not in (None, NO_INDUSTRY):
        return ORDINARY
    return UNKNOWN


def multiple(figs: Figures, close: float | None, name: str) -> float | None:
    """The value multiple `name` of `figs` priced at `close` (module doc), as a yield: the
    lower, the more expensive; None when an input is unknown."""
    if figs.shares is None or close is None:
        return None
    cap = figs.shares * close
    if not cap > 0:
        return None
    if name == "earnings_yield":
        return None if figs.profit is None else figs.profit / cap
    if figs.ebit is None or figs.net_debt is None:
        return None
    value = cap + figs.net_debt
    if value > 0:
        return figs.ebit / value
    return math.inf if figs.ebit > 0 else -math.inf


def _quality(figs: Figures, rule: FundamentalQuality, of: str) -> tuple[str | None, bool]:
    """The first check `figs` fails (None: none) and whether a check lacked its figure; ROCE
    and debt/equity pass for a financial and lack their figure for an unknown kind."""
    corporate = of != FINANCIAL_KIND  # the checks only an ordinary company has
    roce = None if of == UNKNOWN else figs.roce
    gearing = None if of == UNKNOWN else figs.gearing
    checks: list[tuple[str, float | None, Callable[[float], bool], bool]] = [
        (ROCE, roce, lambda v: v < (rule.min_roce or 0), corporate and rule.min_roce is not None),
        (
            GEARING,
            gearing,
            lambda v: v > (rule.max_gearing or 0),
            corporate and rule.max_gearing is not None,
        ),
        (CFO, figs.cfo, lambda v: v <= 0, rule.positive_cfo),
    ]
    missing = False
    for reason, value, fails, on in checks:
        if not on:
            continue
        if value is None:
            missing = True
        elif fails(value):
            return reason, missing
    return None, missing


@dataclass(frozen=True)
class Mark:
    """A name the screen dropped, or kept without a figure or as a financial (`kept`)."""

    name: str
    reason: str
    kept: bool


@dataclass(frozen=True)
class Screen:
    """The names the screen keeps, in their order, and its marks in the names' order."""

    kept: tuple[str, ...]
    marks: tuple[Mark, ...]


def screen(
    names: Sequence[str],
    figs: Callable[[str], Figures | None],
    close: Callable[[str], float | None],
    industry: Callable[[str], str | None],
    rules: Fundamentals,
) -> Screen:
    """The fundamentals screen of `names` (module doc): `figs` each name's figures public by the
    cutoff (and not stale), `close` its decision close on the run's price basis, `industry` its
    industry on the trade date."""
    reasons: dict[str, str] = {}
    missing_reason = MISSING_KEPT if rules.missing == "keep" else MISSING_DROPPED
    found = {n: figs(n) or Figures() for n in names}
    kinds = {n: kind(found[n].family, industry(n)) for n in names}
    if rules.quality is not None:
        for name in names:
            failed, missing = _quality(found[name], rules.quality, kinds[name])
            if failed is not None:
                reasons[name] = failed
            elif missing:
                reasons[name] = missing_reason
    if rules.value is not None:
        _value(names, found, kinds, close, rules.value, reasons, missing_reason)
    marks = []
    for name in names:
        financial = kinds[name] == FINANCIAL_KIND
        reason = reasons.get(name)
        if reason is None and not financial:
            continue
        kept = reason in (None, MISSING_KEPT)
        if reason is None:
            reason = FINANCIAL
        elif financial:
            reason += _FINANCIAL_SUFFIX
        marks.append(Mark(name, reason, kept))
    dropped = {m.name for m in marks if not m.kept}
    return Screen(tuple(n for n in names if n not in dropped), tuple(marks))


def _value(
    names: Sequence[str],
    found: Mapping[str, Figures],
    kinds: Mapping[str, str],
    close: Callable[[str], float | None],
    rule: FundamentalValue,
    reasons: dict[str, str],
    missing_reason: str,
) -> None:
    """The value cut (module doc) of the names the quality floor kept, into `reasons`: under
    `ebit_ev` the financials are a group of their own, ranked on earnings yield; an unknown kind
    has no EBIT/EV."""
    groups: dict[bool, list[tuple[float, str]]] = {False: [], True: []}
    apart = rule.multiple == "ebit_ev"
    for name in names:
        if reasons.get(name, MISSING_KEPT) != MISSING_KEPT:
            continue  # dropped by the quality floor
        financial = kinds[name] == FINANCIAL_KIND
        if apart and kinds[name] == UNKNOWN:
            value = None
        else:
            use = "earnings_yield" if financial else rule.multiple
            value = multiple(found[name], close(name), use)
        if value is None:
            reasons.setdefault(name, missing_reason)
        else:
            groups[apart and financial].append((value, name))
    for ranked in groups.values():
        ranked.sort()
        for _, name in ranked[: int(len(ranked) * rule.drop_pct / 100)]:
            reasons[name] = VALUE


def digest_text(histories: Mapping[str, History]) -> str:
    """The figures a run read, in a fixed order, for its content digest."""
    return repr(
        sorted(
            (name, [(d.isoformat(), repr(x)) for d, x in zip(h.days, h.figures, strict=True)])
            for name, h in histories.items()
        )
    )


def _factors(bars: pl.DataFrame) -> dict[str, tuple[list[date], np.ndarray]]:
    """Each instrument's price-basis factor (the adjusted close over the raw one) by session."""
    frame = (
        bars.select("instrument_id", "date", factor=pl.col("close") / pl.col("raw_close"))
        .filter(pl.col("factor").is_finite() & (pl.col("factor") > 0))
        .sort("instrument_id", "date")
    )
    return {
        str(key[0]): (part["date"].to_list(), part["factor"].to_numpy())
        for key, part in frame.partition_by("instrument_id", as_dict=True).items()
    }


def factor_on(series: tuple[list[date], np.ndarray] | None, day: date) -> float | None:
    """The factor of the last session on or before `day`; None before the first."""
    if series is None:
        return None
    n = bisect_right(series[0], day)
    return float(series[1][n - 1]) if n else None


def load(
    session: Session,
    bars: pl.DataFrame,
    lineage: Mapping[str, str],
    wanted: Collection[str],
    until: date,
) -> dict[str, History]:  # pragma: no cover - the database
    """Each of `wanted` (the run's members, chain heads) with stored filings: its `History`
    through `until`, keyed by the member a company's instruments chain to (`lineage`)."""
    from ..data.fundamentals.research import companies

    heads = set(wanted)
    ids = heads | {old for old, head in lineage.items() if head in heads}
    factors = _factors(bars)
    out: dict[str, History] = {}
    for company in companies(session, ids):
        key = next(
            (h for i in company.ids if (h := lineage.get(i, i)) in heads),
            None,
        )
        if key is None:
            continue
        series = factors.get(key)
        found = history(company, lambda d, s=series: factor_on(s, d), until)
        if found.days:
            out[key] = found
    return out


def snapshot(histories: Mapping[str, History]) -> dict[str, Any]:
    """The data snapshot's count of the figures read (`load_market`)."""
    return {
        "fundamentals": {
            "rule": "the research panel's figures, each from its public date (restated ones"
            " never before their own) plus the strategy's lag in sessions",
            "instruments": len(histories),
            "snapshots": sum(len(h.days) for h in histories.values()),
        }
    }
