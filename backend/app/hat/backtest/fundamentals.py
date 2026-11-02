"""A rotation's point-in-time fundamentals screen (`strategy.config.Fundamentals`).

The figures are the research panel's (`data/fundamentals/research.py`): first-published before
restated, a secondary-source (restated) figure only from its own public date (`research_known`),
each group of figures in the basis with the latest one. `load_market` reads them once for the
run's members (`load`), as a `History` per member: the figures after each date a filing became
public (IST), replayed in order (`history`), so a later version of a filing replaces its
predecessor only from its own public date. Nothing after the period's end is read.

On a decision close, a figure public on day P is usable from the close of the session
`lag_sessions` sessions after P (the session on or after P itself with 0): the screen reads the
latest figures public by `cutoff`. Valuation is priced on the decision close: the share count is
held on the run's price basis (`basis_shares`: the count over the price basis's factor on the day
the figures were taken), so a split or bonus since is in the close it is multiplied by.

The figures (`figures`), floats:
- `roce`: EBIT / (equity + borrowings) at the latest balance sheet; EBIT the TTM's (profit
  before tax + finance cost), else the latest year's; -inf when that capital is not positive.
- `gearing`: borrowings / equity at the latest balance sheet; inf when equity is not positive.
- `cfo`: the latest year's cash flow from operations.
- `profit`: the TTM owners' profit, else the latest year's.
- `ebit`, `net_debt`: as above; borrowings less cash at the latest balance sheet.
A bank (a first-published filing in the bank taxonomy) has no `roce`, `gearing`, `ebit` or
`net_debt` (the store derives no ROCE or debt/equity for one; its EBIT and debt are not a
company's). A missing input leaves a figure unknown (the
store's rule: a nil line is stated as 0).

The screen (`screen`) on the eligible names, in order: `quality` drops a name failing a check
(`ROCE`, `GEARING`, `CFO`, the first failing in that order); a name lacking a figure a check
needs and failing none is kept or dropped by `missing` (`MISSING_KEPT`, `MISSING_DROPPED`). Then
`value` ranks the remaining names with the multiple (`earnings_yield`: profit / market cap;
`ebit_ev`: EBIT / enterprise value, market cap + net debt, +inf for a non-positive value with a
positive EBIT, else -inf) and drops the cheapest-yielding `drop_pct` percent of them, rounded
down (`VALUE`): a loss yields below 0, so it is the most expensive. A name without the multiple
is not ranked and is kept or dropped by `missing`, counted once a night.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
import polars as pl

from ..data.fundamentals import formulas as f
from ..data.fundamentals.book import Book, Reader
from ..data.fundamentals.items import INSTANT
from ..data.fundamentals.research import Action, Company, Known, panel_row
from ..time import IST

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = [
    "CFO",
    "GEARING",
    "MISSING_DROPPED",
    "MISSING_KEPT",
    "ROCE",
    "VALUE",
    "Figures",
    "History",
    "Screen",
    "basis_shares",
    "cutoff",
    "digest_text",
    "factor_on",
    "figures",
    "history",
    "load",
    "multiple",
    "screen",
    "snapshot",
]

# skips.csv reasons (`loop._fundamental_screen`)
ROCE = "fundamentals_roce"
GEARING = "fundamentals_gearing"
CFO = "fundamentals_cfo"
VALUE = "fundamentals_value"
MISSING_KEPT = "fundamentals_missing_kept"
MISSING_DROPPED = "fundamentals_missing_dropped"


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
    """A name's figures as known on one day (module doc); None where unknown."""

    roce: float | None = None
    gearing: float | None = None
    cfo: float | None = None
    profit: float | None = None
    ebit: float | None = None
    net_debt: float | None = None
    shares: float | None = None  # on the run's price basis (`basis_shares`)


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


def figures(
    book: Book,
    day: date,
    actions: Sequence[Action],
    face_value_now: tuple[date, Decimal] | None,
    *,
    bank: bool,
    factor: float | None,
) -> Figures:
    """The figures of `book` at the end of `day` (module doc), the panel's (`panel_row`)."""
    row = panel_row(book, day, None, actions, face_value_now)
    if row is None:
        return Figures()
    profit = row.get("ttm_net_profit")
    if profit is None:
        profit = row.get("net_profit")
    shares = basis_shares(row.get("shares"), factor)
    cfo = _float(row.get("cfo"))
    if bank:
        return Figures(cfo=cfo, profit=_float(profit), shares=shares)
    ebit = row.get("ebit")
    if row.get("ttm_end") is not None:
        r, qe = Reader(book, row["ttm_basis"]), row["ttm_end"]
        ttm = f.ebit(r.ttm(qe, "pbt"), r.ttm(qe, "finance_cost"))
        ebit = ttm if ttm is not None else ebit
    equity, debt = row.get("equity"), row.get("borrowings")
    employed = f.capital_employed(equity, debt)
    roce = None
    if employed is not None and ebit is not None:
        roce = float(ebit / employed) if employed > 0 else -math.inf
    gearing = None
    if equity is not None and equity <= 0:
        gearing = math.inf
    elif equity is not None and debt is not None:
        gearing = float(debt / equity)
    net_debt = None
    if debt is not None and row.get("bs_end") is not None:
        cash = Reader(book, row["bs_basis"]).at(INSTANT, row["bs_end"], "cash")
        net_debt = None if cash is None else float(debt - cash)
    return Figures(
        roce=roce,
        gearing=gearing,
        cfo=cfo,
        profit=_float(profit),
        ebit=_float(ebit),
        net_debt=net_debt,
        shares=shares,
    )


def history(company: Company, factor: Callable[[date], float | None], until: date) -> History:
    """`company`'s figures after each day one of its filings became public, through `until`;
    `factor` the price basis's factor on a day (`basis_shares`)."""
    ordered = sorted(company.known, key=lambda k: (k.at, k.version))
    by_day: dict[date, list[Known]] = {}
    for k in ordered:
        day = k.at.astimezone(IST).date()
        if day <= until:
            by_day.setdefault(day, []).append(k)
    book = Book()
    days: list[date] = []
    out: list[Figures] = []
    for day, filings in by_day.items():
        for k in filings:
            book.add(k.source, k.basis, k.period_end, k.facts)
        found = figures(
            book,
            day,
            company.actions,
            company.face_value_now,
            bank=company.bank,
            factor=factor(day),
        )
        if not out or found != out[-1]:
            days.append(day)
            out.append(found)
    return History(tuple(days), tuple(out))


def cutoff(days: Sequence[date], at: int, lag: int) -> date | None:
    """The last public date a decision on the close of `days[at]` may read under a lag of `lag`
    sessions: `days[at - lag]` (a figure public on day P is read from the close of the
    session `lag` sessions after the first one on or after P); None before the first."""
    return days[at - lag] if at - lag >= 0 else None


def multiple(figs: Figures, close: float | None, kind: str) -> float | None:
    """The value multiple of `figs` priced at `close` (module doc), as a yield: the lower, the
    more expensive; None when an input is unknown."""
    if figs.shares is None or close is None:
        return None
    cap = figs.shares * close
    if not cap > 0:
        return None
    if kind == "earnings_yield":
        return None if figs.profit is None else figs.profit / cap
    if figs.ebit is None or figs.net_debt is None:
        return None
    value = cap + figs.net_debt
    if value > 0:
        return figs.ebit / value
    return math.inf if figs.ebit > 0 else -math.inf


def _quality(figs: Figures | None, rule: FundamentalQuality) -> tuple[str | None, bool]:
    """The first check `figs` fails (None: none) and whether a check lacked its figure."""
    got = figs or Figures()
    missing = False
    checks: list[tuple[str, float | None, Callable[[float], bool], bool]] = [
        (ROCE, got.roce, lambda v: v < (rule.min_roce or 0), rule.min_roce is not None),
        (GEARING, got.gearing, lambda v: v > (rule.max_gearing or 0), rule.max_gearing is not None),
        (CFO, got.cfo, lambda v: v <= 0, rule.positive_cfo),
    ]
    for reason, value, fails, on in checks:
        if not on:
            continue
        if value is None:
            missing = True
        elif fails(value):
            return reason, missing
    return None, missing


@dataclass(frozen=True)
class Screen:
    """The names the screen keeps, in their order, and each name dropped or kept without a
    figure with its reason (`marks`, in the names' order)."""

    kept: tuple[str, ...]
    marks: tuple[tuple[str, str], ...]


def screen(
    names: Sequence[str],
    figs: Callable[[str], Figures | None],
    close: Callable[[str], float | None],
    rules: Fundamentals,
) -> Screen:
    """The fundamentals screen of `names` (module doc): `figs` each name's figures public by the
    cutoff, `close` its decision close on the run's price basis."""
    reasons: dict[str, str] = {}
    keep_missing = rules.missing == "keep"
    missing_reason = MISSING_KEPT if keep_missing else MISSING_DROPPED
    found = {n: figs(n) for n in names}
    if rules.quality is not None:
        for name in names:
            failed, missing = _quality(found[name], rules.quality)
            if failed is not None:
                reasons[name] = failed
            elif missing:
                reasons[name] = missing_reason
    if rules.value is not None:
        _value(names, found, close, rules.value, reasons, missing_reason)
    dropped = {n for n, r in reasons.items() if r != MISSING_KEPT}
    return Screen(
        tuple(n for n in names if n not in dropped),
        tuple((n, reasons[n]) for n in names if n in reasons),
    )


def _value(
    names: Sequence[str],
    found: Mapping[str, Figures | None],
    close: Callable[[str], float | None],
    rule: FundamentalValue,
    reasons: dict[str, str],
    missing_reason: str,
) -> None:
    """The value cut (module doc) of the names the quality floor kept, into `reasons`."""
    ranked: list[tuple[float, str]] = []
    for name in names:
        if reasons.get(name, MISSING_KEPT) != MISSING_KEPT:
            continue  # dropped by the quality floor
        figs = found[name]
        value = None if figs is None else multiple(figs, close(name), rule.multiple)
        if value is None:
            reasons.setdefault(name, missing_reason)
        else:
            ranked.append((value, name))
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
