"""One NSE results XBRL document as plain values: its format, basis, period and the quarter's and
year-to-date figures (plan A2). Pure: bytes in, `Parsed` out.

NSE's instances (Reg 33 and Integrated Filing alike) put the whole statement in two contexts that
the document references but often never defines: `OneD`, the quarter, and `FourD`, the financial
year to date. A2 rule 1: the quarter is read from `OneD` only, never from `FourD`; in a Q3
document `FourD` holds nine months. The period comes from the document's own
`DateOfStartOfReportingPeriod` / `DateOfEndOfReportingPeriod` facts in each context (rule 3);
Reg 33 bank documents give only the end, so the quarter starts three months before it.

Tags (rule 2): revenue is `RevenueFromOperations`, or `InterestEarned` where there is none (banks;
NBFC templates carry both and the first wins). Net profit is the profit attributable to the
owners of the parent where a consolidated document tags it, else the period's profit. EPS is
basic and diluted from continuing and discontinued operations (banks: after extraordinary items).
Every Reg 33 and Integrated variant maps into these same fields (rule 6). A filer's owners'
profit that is in fact the total comprehensive income (it equals `ComprehensiveIncomeForThePeriod`
to within rounding, 0.1%, differs from the period's profit by more, and no non-controlling
interest is tagged) is not the owners'
share: the period's profit is used and the document is flagged (`FLAG_OWNERS_TAG_SWAPPED`).

The quarter context spans a quarter. One spanning more than that is the quarter with a
mislabelled start when the year to date beside it spans twelve months with other figures (flagged,
`FLAG_QUARTER_START_MISLABELLED`); the year itself when it spans twelve months (a Q4 document with
only annual figures, Q4 derived downstream); anything else (a half year) is not read.

Units (rule 5): money facts must be in INR and EPS in INR a share, as declared by the fact's
unit; the values are in rupees whatever the stated rounding level.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET  # noqa: S405 - a doctype or entity is refused first
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ...money import exact

QUARTER_CONTEXT = "OneD"
YTD_CONTEXT = "FourD"
MAX_ABS_VALUE = Decimal(10) ** 14  # numeric(18, 4) holds 14 integer digits

_UNSAFE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_SCHEMA_REF = re.compile(rb"schemaRef[^>]*?href=\"([^\"]+)\"")
_INTEGRATED_NS = re.compile(rb"/xbrl/IntegratedFinance_([A-Za-z]+)/")
_SCHEMA_FORMATS = (
    ("banking_entry_point", "reg33_banking"),
    ("ind-as_entry_point", "reg33_indas"),
    ("other_than_banks_entry_point", "reg33_non_indas"),
    ("in-bse-fin-", "reg33_nbfc"),
)
_INSURER_VARIANTS = ("li", "gi", "insurance", "lifeinsurance", "generalinsurance")
_XBRLI = "{http://www.xbrl.org/2003/instance}"

REVENUE_TAGS = ("RevenueFromOperations", "TotalRevenueFromOperations", "InterestEarned")
BANK_REVENUE_TAGS = ("InterestEarned", "RevenueFromOperations")
INCOME_TAGS = ("Income", "TotalIncome")
OWNERS_PROFIT_TAGS = (
    "ProfitOrLossAttributableToOwnersOfParent",
    "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates",
)
PROFIT_TAGS = (
    "ProfitLossForPeriod",
    "ProfitLossForThePeriod",
    "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates",
)
BASIC_EPS_TAGS = (
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    "BasicEarningsLossPerShareFromContinuingOperations",
    "BasicEarningsPerShareAfterExtraordinaryItems",
    "BasicEarningsPerShareBeforeExtraordinaryItems",
)
DILUTED_EPS_TAGS = (
    "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    "DilutedEarningsLossPerShareFromContinuingOperations",
    "DilutedEarningsPerShareAfterExtraordinaryItems",
    "DilutedEarningsPerShareBeforeExtraordinaryItems",
)
COMPREHENSIVE_INCOME_TAG = "ComprehensiveIncomeForThePeriod"
NCI_PROFIT_TAG = "ProfitOrLossAttributableToNonControllingInterests"
QUARTER_MAX_DAYS = 100  # a quarter spans 89-91 days (end - start)
YEAR_MIN_DAYS = 350  # a financial year spans 364-365
SAME_WITHIN = Decimal("0.001")  # two filed figures this close (relative) are one, rounded
FLAG_QUARTER_START_MISLABELLED = "quarter_start_mislabelled"
FLAG_OWNERS_TAG_SWAPPED = "owners_profit_tag_swapped"
MONEY_UNIT = "iso4217:inr"
EPS_UNIT = "iso4217:inr/xbrli:shares"


# --- classification -------------------------------------------------------------------------


@dataclass(frozen=True)
class XbrlKind:
    raw_format: str  # reg33_* | integrated_* | other | not_xbrl
    format: str  # the store's format (models.results.FORMATS)
    schema_ref: str


def store_format(raw: str) -> str:
    """The store's format of a document format: the three Reg 33 and Integrated variants keep
    their name; insurers are `insurance_skipped`; anything else (Reg 33 non-Ind AS, Integrated
    'other than bank') is `other` and still parsed."""
    if raw in {
        "reg33_indas",
        "reg33_banking",
        "reg33_nbfc",
        "integrated_indas",
        "integrated_banking",
        "integrated_nbfc",
    }:
        return raw
    if raw == "reg33_insurance" or (
        raw.startswith("integrated_") and raw.removeprefix("integrated_") in _INSURER_VARIANTS
    ):
        return "insurance_skipped"
    return "other"


def classify(body: bytes) -> XbrlKind:
    """The taxonomy of an NSE results XBRL instance, from its schemaRef and namespaces."""
    head = body[:20000]
    ref = _SCHEMA_REF.search(head)
    schema = ref.group(1).decode(errors="replace") if ref else ""
    if not schema or b"xbrl" not in head[:2000]:
        return XbrlKind("not_xbrl", "other", schema)
    integrated = _INTEGRATED_NS.search(head)
    if integrated:
        raw = f"integrated_{integrated.group(1).decode().lower()}"
    elif b"/xbrl/Insurance/" in head:
        raw = "reg33_insurance"
    else:
        lowered = schema.lower()
        raw = next(
            (kind for prefix, kind in _SCHEMA_FORMATS if lowered.startswith(prefix)), "other"
        )
    return XbrlKind(raw, store_format(raw), schema)


# --- facts ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fact:
    text: str
    unit: str  # the unit's measures, lower case ("iso4217:inr"), "" for a non-numeric fact


Facts = dict[str, dict[str, Fact]]  # context id -> local name -> fact (the first one kept)


class XbrlError(ValueError):
    """A document that cannot be read; the message is the exclusion reason."""


class _NoDoctype(ET.TreeBuilder):
    """A tree builder that refuses a doctype where the parser meets it: anywhere in the
    document and in any encoding, before any entity it declares is expanded."""

    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise XbrlError("xbrl_doctype")


def read_facts(body: bytes) -> tuple[Facts, dict[str, tuple[date | None, date | None]]]:
    """Every fact by context, and each defined context's period (start, end; an instant has
    only the end)."""
    if _UNSAFE.search(body):
        raise XbrlError("xbrl_doctype")
    try:
        root = ET.fromstring(body, parser=ET.XMLParser(target=_NoDoctype()))  # noqa: S314
    except ET.ParseError as exc:
        raise XbrlError("xbrl_malformed") from exc
    units: dict[str, str] = {}
    periods: dict[str, tuple[date | None, date | None]] = {}
    for unit in root.iter(f"{_XBRLI}unit"):
        measures = [(m.text or "").strip().lower() for m in unit.iter(f"{_XBRLI}measure")]
        units[unit.get("id", "")] = "/".join(measures)
    for context in root.iter(f"{_XBRLI}context"):
        period = context.find(f"{_XBRLI}period")
        if period is None:
            continue
        start = _date(period.findtext(f"{_XBRLI}startDate"))
        end = _date(period.findtext(f"{_XBRLI}endDate") or period.findtext(f"{_XBRLI}instant"))
        periods[context.get("id", "")] = (start, end)
    facts: Facts = {}
    for element in root:
        ref = element.get("contextRef")
        if ref is None:
            continue
        name = element.tag.rsplit("}", 1)[-1]
        unit = units.get(element.get("unitRef") or "", "")
        facts.setdefault(ref, {}).setdefault(name, Fact((element.text or "").strip(), unit))
    return facts, periods


def _date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text.strip()[:10])
    except ValueError:
        return None


def _number(fact: Fact | None, unit: str) -> Decimal | None:
    if fact is None or not fact.text:
        return None
    if fact.unit != unit:
        raise XbrlError("unit_not_inr" if unit == MONEY_UNIT else "unit_not_inr_per_share")
    value = exact(fact.text)
    if value is None:
        raise XbrlError("value_not_numeric")
    if abs(value) >= MAX_ABS_VALUE:
        raise XbrlError("value_out_of_range")
    return value


def _first(context: dict[str, Fact], tags: tuple[str, ...], unit: str) -> Decimal | None:
    for tag in tags:
        value = _number(context.get(tag), unit)
        if value is not None:
            return value
    return None


# --- figures --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Figures:
    revenue: Decimal | None
    total_income: Decimal | None
    net_profit: Decimal | None
    eps_basic: Decimal | None
    eps_diluted: Decimal | None

    def minus(self, other: Figures) -> Figures:
        """These figures less `other`'s, field by field (None where either is None)."""

        def sub(a: Decimal | None, b: Decimal | None) -> Decimal | None:
            return None if a is None or b is None else a - b

        return Figures(
            sub(self.revenue, other.revenue),
            sub(self.total_income, other.total_income),
            sub(self.net_profit, other.net_profit),
            sub(self.eps_basic, other.eps_basic),
            sub(self.eps_diluted, other.eps_diluted),
        )


def figures(context: dict[str, Fact], kind: str, basis: str) -> Figures:
    """One context's figures under the tag rules above (`kind` the store format)."""
    banking = kind.endswith("banking")
    revenue = _first(context, BANK_REVENUE_TAGS if banking else REVENUE_TAGS, MONEY_UNIT)
    period_profit = _first(context, PROFIT_TAGS, MONEY_UNIT)
    profit = period_profit
    if basis == "consolidated" and not owners_tag_swapped(context):
        owners = _first(context, OWNERS_PROFIT_TAGS, MONEY_UNIT)
        # a template field left at 0 while the period shows a profit is not the owners' share
        if owners is not None and (owners != 0 or not period_profit):
            profit = owners
    return Figures(
        revenue,
        _first(context, INCOME_TAGS, MONEY_UNIT),
        profit,
        _first(context, BASIC_EPS_TAGS, EPS_UNIT),
        _first(context, DILUTED_EPS_TAGS, EPS_UNIT),
    )


def owners_tag_swapped(context: dict[str, Fact]) -> bool:
    """Whether the context's owners' profit is the total comprehensive income (rule above)."""
    owners = _first(context, OWNERS_PROFIT_TAGS, MONEY_UNIT)  # the tag `figures` would read
    comprehensive = _first(context, (COMPREHENSIVE_INCOME_TAG,), MONEY_UNIT)
    period_profit = _first(context, PROFIT_TAGS, MONEY_UNIT)
    nci = _first(context, (NCI_PROFIT_TAG,), MONEY_UNIT)
    if owners is None or comprehensive is None or period_profit is None or nci:
        return False
    return _same_figure(owners, comprehensive) and not _same_figure(owners, period_profit)


def _same_figure(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= SAME_WITHIN * max(abs(a), abs(b))


# --- the document ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Parsed:
    kind: XbrlKind
    basis: str  # from NatureOfReportStandaloneConsolidated; "" when absent
    period_start: date | None
    period_end: date | None
    quarter: Figures | None  # None for an insurer, or a Q4 document with only the year
    ytd_start: date | None
    ytd: Figures | None
    annual: Figures | None  # a Q4 document with only the year: the year's figures
    nine_month: Figures | None  # ... and nine months, when the document carries them
    fy_start: date | None
    flags: tuple[str, ...] = ()  # FLAG_* the document's figures were read under


def quarter_start(end: date) -> date:
    """The first day of the three-month quarter ending on `end` (a month end)."""
    month = end.month - 2
    year = end.year
    if month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def _span_days(start: date | None, end: date | None) -> int | None:
    return None if start is None or end is None else (end - start).days


def _period(
    context: dict[str, Fact], defined: tuple[date | None, date | None] | None
) -> tuple[date | None, date | None]:
    start = _date(_text(context, "DateOfStartOfReportingPeriod"))
    end = _date(_text(context, "DateOfEndOfReportingPeriod"))
    if defined is not None:
        start = start or defined[0]
        end = end or defined[1]
    return start, end


def _text(context: dict[str, Fact], name: str) -> str:
    fact = context.get(name)
    return fact.text if fact else ""


def basis_of_document(facts: Facts) -> str:
    for ref in (QUARTER_CONTEXT, YTD_CONTEXT):
        text = _text(facts.get(ref, {}), "NatureOfReportStandaloneConsolidated").lower()
        if text.startswith("consolidated"):
            return "consolidated"
        if text.startswith("standalone"):
            return "standalone"
    return ""


def parse(body: bytes) -> Parsed:
    """The document's format, basis, period and figures; raises `XbrlError` (its message the
    exclusion reason) when the quarter cannot be read."""
    kind = classify(body)
    if kind.raw_format == "not_xbrl":
        raise XbrlError("not_xbrl")
    facts, periods = read_facts(body)
    basis = basis_of_document(facts)
    one = facts.get(QUARTER_CONTEXT, {})
    four = facts.get(YTD_CONTEXT, {})
    start, end = _period(one, periods.get(QUARTER_CONTEXT))
    ytd_start, ytd_end = _period(four, periods.get(YTD_CONTEXT))
    fy_start = _date(_text(one, "DateOfStartOfFinancialYear"))
    ytd_start = ytd_start or (fy_start if ytd_end else None)
    blank = Parsed(kind, basis, start, end, None, ytd_start, None, None, None, fy_start)
    if kind.format == "insurance_skipped":
        return blank
    if end is None:
        raise XbrlError("period_missing")
    if not one:
        raise XbrlError("quarter_context_missing")
    quarter = figures(one, kind.format, basis)
    ytd = figures(four, kind.format, basis) if four else None
    swapped = basis == "consolidated" and any(owners_tag_swapped(c) for c in (one, four) if c)
    flags = (FLAG_OWNERS_TAG_SWAPPED,) if swapped else ()
    span = _span_days(start, end)
    if span is not None and QUARTER_MAX_DAYS < span < YEAR_MIN_DAYS:
        if not _mislabelled_start(ytd_start, ytd_end, quarter, ytd):
            raise XbrlError("quarter_context_not_a_quarter")
        flags = (FLAG_QUARTER_START_MISLABELLED, *flags)
        return Parsed(
            kind,
            basis,
            quarter_start(end),
            end,
            quarter,
            ytd_start,
            ytd,
            None,
            None,
            fy_start,
            flags,
        )
    if _annual_only(start, end, fy_start, ytd_start, quarter, ytd):
        nine = _nine_month(facts, periods, kind.format, basis, end)
        q_start = quarter_start(end)
        return Parsed(
            kind,
            basis,
            q_start,
            end,
            None,
            fy_start or start,
            quarter,
            quarter,
            nine,
            fy_start,
            flags,
        )
    q_start = start or quarter_start(end)
    return Parsed(kind, basis, q_start, end, quarter, ytd_start, ytd, None, None, fy_start, flags)


def _mislabelled_start(
    ytd_start: date | None, ytd_end: date | None, quarter: Figures, ytd: Figures | None
) -> bool:
    """Whether a quarter context longer than a quarter is the quarter with a mislabelled start:
    the year to date beside it spans twelve months and holds other figures."""
    span = _span_days(ytd_start, ytd_end)
    return span is not None and span >= YEAR_MIN_DAYS and ytd is not None and ytd != quarter


def _annual_only(
    start: date | None,
    end: date,
    fy_start: date | None,
    ytd_start: date | None,
    quarter: Figures,
    ytd: Figures | None,
) -> bool:
    """Whether the quarter context holds the whole year (a Q4 document with only annual
    figures): it spans twelve months, or it starts the financial year, ends the year and equals
    the year to date (in Q1 the two are equal legitimately, but Q1 does not end a year)."""
    span = _span_days(start, end)
    if span is not None:
        return span >= YEAR_MIN_DAYS
    year_start = fy_start or ytd_start
    if year_start is None or (end - year_start).days < 300 or ytd is None:
        return False
    return quarter == ytd


def _nine_month(
    facts: Facts,
    periods: dict[str, tuple[date | None, date | None]],
    kind: str,
    basis: str,
    end: date,
) -> Figures | None:
    """The nine months to the end of Q3 when the document carries them in some context."""
    for ref, context in facts.items():
        if ref in (QUARTER_CONTEXT, YTD_CONTEXT):
            continue
        start, stop = _period(context, periods.get(ref))
        span = _span_days(start, stop)
        if span is not None and 250 <= span <= 290 and stop is not None:
            if 88 <= (end - stop).days <= 92:
                found = figures(context, kind, basis)
                if found.revenue is not None:
                    return found
    return None
