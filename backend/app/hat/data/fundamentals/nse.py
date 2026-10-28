"""Every numeric fact of one NSE results XBRL document for its reporting period. Pure: bytes in,
`Document` out.

The document's period is the one `results/xbrl.py` reads (its own `DateOfStart/EndOfReporting
Period` facts in `OneD`, the quarter; `FourD` the financial year to date). A context is kept when
it is about that period: `OneD`/`OneI` (the quarter and the balance sheet as at its end), `FourD`/
`FourI` (the year to date, which holds the half-year cash flow statement), and the dimensional
contexts named after them (`OneReportableSegmentRevenue01D` takes `OneD`'s period). Any other
context (a prior year's balance sheet `PY_I`, an auditor's) is kept only when its own period ends
on the document's period end. A main duration context spanning more than 100 days is the year to
date (`annual` when it spans a year); a Q4 document with only the year carries the year in `OneD`.

Values are in rupees (EPS in rupees a share) as the fact's unit declares, whatever the rounding
level; a fact in an unknown unit is kept with the unit's own name.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ...money import exact
from ..results.xbrl import (
    QUARTER_CONTEXT,
    YTD_CONTEXT,
    XbrlError,
    XbrlKind,
    basis_of_document,
    classify,
    quarter_start,
    read_facts,
    reporting_period,
)
from .items import ANNUAL, INSTANT, QUARTER, XBRL_UNITS, YTD, nse_items, nse_statement

QUARTER_INSTANT = "OneI"
YTD_INSTANT = "FourI"
MAIN_CONTEXTS = (QUARTER_CONTEXT, YTD_CONTEXT, QUARTER_INSTANT, YTD_INSTANT)
MAX_QUARTER_DAYS = 100
MIN_YEAR_DAYS = 350
MAX_ABS_VALUE = Decimal(10) ** 20

FAMILIES = (
    ("banking", "bank"),
    ("nbfc", "nbfc"),
    ("insurance", "insurer"),
    ("_li", "insurer"),
    ("_gi", "insurer"),
    ("indas", "indas"),
)


@dataclass(frozen=True)
class Fact:
    statement: str
    period_kind: str
    period_start: date | None
    period_end: date
    element: str
    context: str
    item: str | None
    value: Decimal
    unit: str


@dataclass(frozen=True)
class Document:
    kind: XbrlKind
    family: str  # indas | nbfc | bank | insurer | other
    basis: str  # "" when the document does not state it
    period_start: date | None
    period_end: date
    facts: tuple[Fact, ...]


def family(raw_format: str) -> str:
    """The taxonomy family of a document format (`results.xbrl.classify`)."""
    return next((name for key, name in FAMILIES if key in raw_format), "other")


def period_kind(start: date | None, end: date) -> str:
    if start is None:
        return INSTANT
    span = (end - start).days
    if span <= MAX_QUARTER_DAYS:
        return QUARTER
    return ANNUAL if span >= MIN_YEAR_DAYS else YTD


def _main_of(context: str) -> str | None:
    """The main context a dimensional context is named after (`One...D` takes `OneD`)."""
    for prefix in ("One", "Four"):
        if context.startswith(prefix) and context not in MAIN_CONTEXTS:
            suffix = "D" if context.endswith("D") else "I" if context.endswith("I") else ""
            return f"{prefix}{suffix}" if suffix else None
    return None


def parse_document(body: bytes) -> Document:
    """The document's numeric facts for its reporting period; raises `XbrlError` (its message the
    reason) when it is not an XBRL instance or states no period."""
    kind = classify(body)
    if kind.raw_format == "not_xbrl":
        raise XbrlError("not_xbrl")
    facts, defined = read_facts(body)
    one = facts.get(QUARTER_CONTEXT, {})
    start, end = reporting_period(one, defined.get(QUARTER_CONTEXT))
    if end is None:
        start, end = reporting_period(facts.get(YTD_CONTEXT, {}), defined.get(YTD_CONTEXT))
    if end is None:
        raise XbrlError("period_missing")
    ytd_start, _ytd_end = reporting_period(facts.get(YTD_CONTEXT, {}), defined.get(YTD_CONTEXT))
    fy_start = _date_fact(one, "DateOfStartOfFinancialYear")
    periods: dict[str, tuple[date | None, date]] = {
        QUARTER_CONTEXT: (start or quarter_start(end), end),
        YTD_CONTEXT: (ytd_start or fy_start or start or quarter_start(end), end),
        QUARTER_INSTANT: (None, end),
        YTD_INSTANT: (None, end),
    }
    out: list[Fact] = []
    for context, values in facts.items():
        main = context if context in MAIN_CONTEXTS else _main_of(context)
        if main is not None and main in periods:
            p_start, p_end = periods[main]
        else:
            p_start, p_end = defined.get(context, (None, None))
            if p_end != end:
                continue  # another period's figures (a comparative), or undated
        assert p_end is not None
        dimensional = context not in MAIN_CONTEXTS
        mapped = {} if dimensional else nse_items(set(values))
        for element, fact in values.items():
            value = _value(fact.text) if fact.unit else None
            if value is None:
                continue
            unit = XBRL_UNITS.get(fact.unit, fact.unit)
            instant = p_start is None
            out.append(
                Fact(
                    nse_statement(element, unit, instant, dimensional),
                    period_kind(p_start, p_end),
                    p_start,
                    p_end,
                    element,
                    context,
                    mapped.get(element),
                    value,
                    unit,
                )
            )
    return Document(
        kind,
        family(kind.raw_format),
        basis_of_document(facts),
        periods[QUARTER_CONTEXT][0],
        end,
        tuple(out),
    )


def _value(text: str) -> Decimal | None:
    value = exact(text)
    if value is None or abs(value) >= MAX_ABS_VALUE:
        return None
    return value


def _date_fact(context: dict, name: str) -> date | None:
    fact = context.get(name)
    if fact is None or not fact.text:
        return None
    try:
        return date.fromisoformat(fact.text.strip()[:10])
    except ValueError:
        return None


def fact_key(facts: tuple[Fact, ...]) -> frozenset[tuple[str, str, Decimal]]:
    """What a document says, for telling a revision that repeats its predecessor."""
    return frozenset((f.context, f.element, f.value.normalize()) for f in facts)
