"""NSE's HTML results pages (`archives/financial_results/financial_res_<SYMBOL>_<seq>.html`): the
first-published results of every filing before XBRL (to 2018). Pure.

A page has a header table (label cells `tablehead`, values `t1`: symbol, basis, period, relating
to, financial year, period ended) and one body table of label (`t1`) and value (`t0`) cells under
section headings (cells spanning both columns). Rows are not always closed, so cells are read in
order and paired by class. Amounts are in lakh rupees (the body's `Amount(Rs. in lakhs)`), EPS
and face value in rupees, percentages as stated. Each value becomes one fact: the label is its
`element`, the section its `context`, and the canonical item where `HTML_ITEMS` maps the label.

Periods: a `Quarterly` page is its quarter; an `Annual` page (relating to `Annual`) its year, and
its reserves (excluding revaluation reserves) and paid-up capital the balances at the year end
(`reserves`, `share_capital`). A quarterly page's reserves line is the previous year end's balance
and is kept raw, as is a nil reserves line (left unfilled). A quarter's paid-up capital and face
value are as at its end (`quarter`, like the XBRL documents, so `paid_up_capital / face_value`
gives shares the same way). Figures are as filed: some filers typed crores into the lakh column
of the reserves line, so a year's book value is checked against the secondary source before
research use (`research.py`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from html.parser import HTMLParser

from ...money import exact
from .items import ANNUAL, INSTANT, QUARTER

LAKH = Decimal(10) ** 5
FORMAT = "html"
FAMILY_BANK = "bank"
FAMILY_OTHER = "html"

# (item, pattern) in priority order: a label (lower case, spaces collapsed, its enumerator such
# as `(a)` or `ii.` dropped) gives the first item whose pattern matches it whole; a page keeps
# one fact per item, the one of the earliest pattern
_PROFIT = r"net profit ?/ ?\(loss\)|net profit ?\(\+\) ?/ ?loss ?\(-\)|net profit ?/ ?loss"
HTML_ITEMS: tuple[tuple[str, str], ...] = (
    ("revenue", r"total income from operations ?\(net\).*|revenue from operations.*"),
    ("revenue", r"interest earned.*"),
    ("revenue", r"net sales ?/ ?income from operations.*"),
    ("other_income", r"other income"),
    ("total_income", r"total income"),
    ("material_cost", r"cost of materials consumed|consumption of raw materials"),
    ("purchases_stock", r"purchases? of (stock-in-trade|traded goods)"),
    ("inventory_change", r"changes? in inventor(y|ies).*|\(increase\) ?/ ?decrease in stock.*"),
    ("employee_cost", r"employee(s)? (benefits? )?(expenses?|cost)"),
    ("depreciation", r"depreciation( and amortisation)?( expenses?)?"),
    ("finance_cost", r"finance costs?|interest expended|interest"),
    ("provisions", r"provisions \(other than tax\) and contingencies"),
    ("exceptional_items", r"exceptional items"),
    ("pbt", r"profit ?/ ?\(loss\) from ordinary activities before tax"),
    ("pbt", r"profit ?\(\+\) ?/ ?loss ?\(-\) from ordinary activities before tax"),
    ("pbt", r"profit ?/ ?\(loss\) before tax|profit before tax"),
    ("tax", r"tax expenses?( \(.*\))?"),
    ("net_profit", rf"({_PROFIT}) for the period"),
    ("net_profit_owners", r"net profit ?/ ?\(loss\) after taxes, minority interest and share of.*"),
    (
        "net_profit_owners",
        r"consolidated net profit ?(\(\+\) ?/ ?loss ?\(-\)|/ ?loss) for the period",
    ),
    ("paid_up_capital", r"paid[- ]up equity share capital.*"),
    ("face_value", r"face value.*"),
    ("reserves", r"reserves? excluding revaluation reserves?.*"),
    ("eps_basic", r"basic eps (for continued and discontinued operations|before extraordinary.*)"),
    ("eps_basic", r"basic eps for continuing operations"),
    ("eps_diluted", r"diluted eps (for continued and discontinued.*|before extraordinary.*)"),
    ("eps_diluted", r"diluted eps for continuing operations"),
    ("eps_basic", r"basic"),  # under 'Earnings per share (before extraordinary items)'
    ("eps_diluted", r"diluted"),
)
_COMPILED = tuple((item, re.compile(pattern)) for item, pattern in HTML_ITEMS)
_ENUMERATOR = re.compile(r"^(\(?[a-z]{1,4}\)|[a-z]{1,4}\.|\(?\d{1,2}\)|\d{1,2}\.|-)\s*")
# the P&L items a statement carries (others: `other`)
_PL = {
    "revenue",
    "other_income",
    "total_income",
    "material_cost",
    "purchases_stock",
    "inventory_change",
    "employee_cost",
    "depreciation",
    "finance_cost",
    "provisions",
    "exceptional_items",
    "pbt",
    "tax",
    "net_profit",
    "net_profit_owners",
    "eps_basic",
    "eps_diluted",
}
_PER_SHARE = {"eps_basic", "eps_diluted", "face_value"}
_EPS_BEFORE = "before extraordinary"  # the EPS section whose basic/diluted lines are kept
_SEGMENTS = "segment"


class HtmlPageError(ValueError):
    """A page that is not a readable results page (a reason, as an outcome name)."""


@dataclass(frozen=True)
class HtmlFact:
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
class HtmlPage:
    symbol: str
    basis: str  # consolidated | standalone
    period_end: date
    annual: bool
    family: str  # bank | html
    facts: tuple[HtmlFact, ...]


@dataclass(frozen=True)
class _Cell:
    cls: str
    span: bool
    text: str


class _Cells(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[_Cell] = []
        self._open: tuple[str, bool] | None = None
        self._text: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("td", "th"):
            self._close()
            values = dict(attrs)
            self._open = ((values.get("class") or "").lower(), "colspan" in values)
        elif tag == "tr":
            self._close()

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th", "tr", "table"):
            self._close()

    def handle_data(self, data: str) -> None:
        if self._open is not None and not self._skip:
            self._text.append(data)

    def _close(self) -> None:
        if self._open is not None:
            text = " ".join("".join(self._text).split())
            self.cells.append(_Cell(self._open[0], self._open[1], text))
        self._open = None
        self._text = []


def cells(body: bytes) -> list[_Cell]:
    parser = _Cells()
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    parser._close()
    return parser.cells


def normal(label: str) -> str:
    return " ".join(label.lower().split())


def number(text: str) -> Decimal | None:
    """A value cell's number (`1,234.50`, `-12`, `(12)`); None for `-`, blank or text."""
    raw = text.strip()
    negative = raw.startswith("(") and raw.endswith(")")
    value = exact(raw.strip("()"), grouped=True)
    if value is None:
        return None
    return -value if negative else value


def label_key(label: str) -> str:
    """A label as matched: lower case, spaces collapsed, its enumerator dropped."""
    return _ENUMERATOR.sub("", normal(label), count=1)


def item_of(label: str, section: str) -> tuple[str, int] | None:
    """The canonical item of a body label and its pattern's rank (`HTML_ITEMS`), or None.
    Segment lines map to nothing; a bare `basic`/`diluted` line only in the before-extraordinary
    EPS section."""
    where = normal(section)
    if _SEGMENTS in where:
        return None
    text = label_key(label)
    for rank, (item, pattern) in enumerate(_COMPILED):
        if pattern.fullmatch(text):
            if text in ("basic", "diluted") and _EPS_BEFORE not in where:
                return None
            return item, rank
    return None


def unit_of(item: str | None, label: str, section: str) -> str:
    """The unit of a value: rupees a share (EPS, face value), shares, a percentage or ratio as
    stated (`pure`), a count (investor complaints), else lakh rupees (`inr` once scaled)."""
    text, where = normal(label), normal(section)
    if item in _PER_SHARE or " eps" in f" {text}" or "earnings per share" in where:
        return "inr_per_share"
    if "number of shares" in text:
        return "shares"
    if re.search(r"percentage|^%|\(%\)|%\s*$|\bratio\b|return on assets", text):
        return "pure"
    if "complaints" in where or text.endswith(("quarter", "quarter end")):
        return "count"
    return "inr"


def _date(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip().title(), "%d-%b-%Y").date()
    except ValueError:
        return None


def basis_of(text: str) -> str:
    value = normal(text)
    if value.startswith("non"):
        return "standalone"
    if value.startswith("consolidated"):
        return "consolidated"
    return ""


def quarter_start(end: date) -> date:
    month = end.month - 2
    year = end.year
    if month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def dated(page: HtmlPage, listed_end: date, broadcast: datetime | None) -> HtmlPage:
    """`page` with the listing's period end where the page header names another and the listing's
    is before the broadcast (a header left at last year's date: the listing is what the market
    saw the filing as); else as parsed."""
    if page.period_end == listed_end or broadcast is None or listed_end >= broadcast.date():
        return page
    start = quarter_start(listed_end)
    facts = []
    for x in page.facts:
        moved = x.period_start
        if x.period_start is not None:
            moved = (
                date(
                    x.period_start.year + listed_end.year - page.period_end.year,
                    x.period_start.month,
                    x.period_start.day,
                )
                if page.annual
                else start
            )
        facts.append(replace(x, period_end=listed_end, period_start=moved))
    return replace(page, period_end=listed_end, facts=tuple(facts))


def parse_page(body: bytes) -> HtmlPage:
    """One results page's facts (module doc); `HtmlPageError` when it is not one."""
    found = cells(body)
    header: dict[str, str] = {}
    key: str | None = None
    label: str | None = None
    section = ""
    family = FAMILY_OTHER
    raw: list[tuple[str, str, str]] = []  # (section, label, value)
    for cell in found:
        if cell.cls == "tablehead":
            key = normal(cell.text)
            continue
        if key is not None and cell.cls == "t1":
            header.setdefault(key, cell.text)
            key = None
            continue
        key = None
        if cell.cls == "t1":
            label = cell.text
        elif cell.cls == "t0" and label is not None:
            raw.append((section, label, cell.text))
            label = None
        elif cell.text and (cell.span or not cell.cls):
            text = normal(cell.text)
            if text and not text.startswith(("description", "non banking", "banking")):
                section = cell.text
            label = None
    symbol = header.get("symbol", "")
    end = _date(header.get("period ended", ""))
    basis = basis_of(header.get("consolidated / non-consolidated", ""))
    if not symbol or end is None:
        raise HtmlPageError("html_no_header")
    if not basis:
        raise HtmlPageError("html_basis_unknown")
    if not raw:
        raise HtmlPageError("html_no_figures")
    annual = normal(header.get("relating to", "")) == "annual"
    if any(label_key(label).startswith("interest earned") for _w, label, _v in raw):
        family = FAMILY_BANK  # the banking format (its revenue line)
    year = header.get("financial year", "")
    year_start = _date(year.split(" To ")[0]) if " To " in year else None
    start = year_start if annual else quarter_start(end)
    best: dict[str, int] = {}
    kept: list[tuple[str, str, str | None, int, Decimal]] = []
    for where, text, value_text in raw:
        value = number(value_text)
        if value is None:
            continue
        mapped = item_of(text, where)
        item, rank = mapped if mapped else (None, 0)
        if item == "reserves" and (not annual or value == 0):
            item = None  # the previous year end's balance, or a line left at nil (module doc)
        if item == "paid_up_capital" and annual:
            item = "share_capital"  # the year end's balance, beside its reserves
        if item is not None:
            if item in best and best[item] <= rank:
                item = None  # a later or lower line of an item already given: kept raw
            else:
                best[item] = rank
        kept.append((where, text, item, rank, value))
    facts = []
    for where, text, item, rank, value in kept:
        if item is not None and best.get(item) != rank:
            item = None  # displaced by a better line further down
        unit = unit_of(item, text, where)
        amount = value * LAKH if unit == "inr" else value
        if item in _PL:
            statement, kind, period_start = "pl", ANNUAL if annual else QUARTER, start
        elif item in ("reserves", "share_capital"):
            statement, kind, period_start = "bs", INSTANT, None
        else:
            statement = "segment" if _SEGMENTS in normal(where) else "other"
            kind, period_start = (ANNUAL if annual else QUARTER), start
        facts.append(HtmlFact(statement, kind, period_start, end, text, where, item, amount, unit))
    return HtmlPage(symbol, basis, end, annual, family, tuple(facts))
