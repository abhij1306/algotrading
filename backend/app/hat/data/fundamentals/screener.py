"""Every figure of one secondary-source (screener.in) company page. Pure: bytes in, `Page` out.

A page shows the figures as they stood when it was fetched (restated where the company restated
them), so they are not the point-in-time originals. Its sections: `quarters` (the latest 13
quarters), `profit-loss` (the years, about 10-12, and a TTM column that is not read: TTM is
derived), `balance-sheet` (as at each year end), `cash-flow` (the years), `ratios` (the first
table: working-capital days and ROCE or ROE per year; the later tables need a login and show
placeholders) and `shareholding` (the quarterly table, then the yearly one for older year ends).
`top-ratios` is the summary at fetch time (market cap, price, P/E, book value, face value).

Units: money in crore rupees, converted to rupees; EPS, price, book and face value in rupees a
share; a percentage becomes a fraction; days are days. Each section names its basis above the
table ('Consolidated Figures in Rs. Crores / View Standalone'); shareholding names none.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser

from ...money import exact
from ..results.secondary import named_basis
from .items import (
    ANNUAL,
    COUNT,
    DAYS,
    FRACTION,
    INR,
    INR_PER_SHARE,
    INSTANT,
    PURE,
    QUARTER,
    SCREENER_ITEMS,
    SCREENER_STATEMENT,
    screener_label,
)

CRORE = Decimal(10) ** 7
HUNDRED = Decimal(100)
SECTIONS = ("quarters", "profit-loss", "balance-sheet", "cash-flow", "ratios", "shareholding")
TOP_RATIOS = "top-ratios"
SECTION_KIND = {
    "quarters": QUARTER,
    "profit-loss": ANNUAL,
    "cash-flow": ANNUAL,
    "ratios": ANNUAL,
    "balance-sheet": INSTANT,
    "shareholding": INSTANT,
    TOP_RATIOS: INSTANT,
}
# top-ratios: label -> unit of the number shown (market cap in crore)
TOP_UNITS = {
    "market cap": INR,
    "current price": INR_PER_SHARE,
    "stock p/e": PURE,
    "book value": INR_PER_SHARE,
    "dividend yield": FRACTION,
    "roce": FRACTION,
    "roe": FRACTION,
    "face value": INR_PER_SHARE,
}
_MONTHS = {name.lower(): n for n, name in enumerate(calendar.month_abbr) if name}


@dataclass(frozen=True)
class Figure:
    section: str
    statement: str
    period_kind: str
    period_end: date
    label: str  # the row label as the page shows it
    item: str | None
    value: Decimal
    unit: str
    basis: str  # "" when the section names none


@dataclass
class _Table:
    header: list[tuple[str, str | None]] = field(default_factory=list)  # (text, data-date-key)
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class _Section:
    sub: str = ""
    tables: list[_Table] = field(default_factory=list)


class _PageParser(HTMLParser):
    """The text of each wanted section's tables (header cells with their date keys, body rows)
    and its sub line, and the name/number pairs of `top-ratios`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: dict[str, _Section] = {}
        self.top: list[tuple[str, str]] = []
        self.current: _Section | None = None
        self.depth = 0
        self.in_sub = False
        self.cell: list[str] | None = None
        self.cell_key: str | None = None
        self.row: list[str] | None = None
        self.in_head = False
        self.in_top = False
        self.top_part: str | None = None  # "name" | "number"
        self.top_name: list[str] = []
        self.top_number: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if tag == "ul" and values.get("id") == TOP_RATIOS:
            self.in_top = True
        elif self.in_top:
            self._top_start(tag, classes)
        if tag == "section":
            if self.current is not None:
                self.depth += 1
            elif values.get("id") in SECTIONS:
                self.current = self.sections.setdefault(values["id"] or "", _Section())
                self.depth = 1
            return
        if self.current is None:
            return
        if tag == "p" and "sub" in classes and not self.current.sub:
            self.in_sub = True
        elif tag == "table":
            self.current.tables.append(_Table())
        elif tag == "thead":
            self.in_head = True
        elif tag == "tr":
            self.row = []
        elif tag in ("th", "td") and self.row is not None:
            self.cell = []
            self.cell_key = values.get("data-date-key")

    def _top_start(self, tag: str, classes: list[str]) -> None:
        if tag == "li":
            self.top_name, self.top_number = [], []
        elif tag == "span" and "name" in classes:
            self.top_part = "name"
        elif tag == "span" and "number" in classes and not self.top_number:
            self.top_part = "number"

    def handle_endtag(self, tag: str) -> None:
        if self.in_top:
            if tag == "span":
                self.top_part = None
            elif tag == "li":
                self.top.append(
                    (" ".join("".join(self.top_name).split()), "".join(self.top_number))
                )
            elif tag == "ul":
                self.in_top = False
        if self.current is None:
            return
        if tag == "section":
            self.depth -= 1
            if not self.depth:
                self.current = None
        elif tag == "p":
            self.in_sub = False
        elif tag in ("th", "td") and self.cell is not None and self.row is not None:
            text = " ".join("".join(self.cell).split())
            if self.in_head and self.current.tables:
                self.current.tables[-1].header.append((text, self.cell_key))
            else:
                self.row.append(text)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if not self.in_head and self.row and self.current.tables:
                self.current.tables[-1].rows.append(self.row)
            self.row = None
        elif tag == "thead":
            self.in_head = False

    def handle_data(self, data: str) -> None:
        if self.in_top and self.top_part == "name":
            self.top_name.append(data)
        elif self.in_top and self.top_part == "number":
            self.top_number.append(data)
        if self.current is None:
            return
        if self.in_sub:
            self.current.sub += data
        if self.cell is not None:
            self.cell.append(data)


def column_date(text: str, key: str | None) -> date | None:
    """A column's period end: its `data-date-key`, else its 'Mon YYYY' heading as that month's
    last day; None for a TTM or unreadable column."""
    if key:
        try:
            return date.fromisoformat(key)
        except ValueError:
            return None
    parts = text.split()
    if len(parts) != 2 or parts[0][:3].lower() not in _MONTHS or not parts[1].isdigit():
        return None
    year, month = int(parts[1]), _MONTHS[parts[0][:3].lower()]
    return date(year, month, calendar.monthrange(year, month)[1])


def cell_value(section: str, label: str, text: str) -> tuple[Decimal, str] | None:
    """A cell's value and unit in rupees (a crore figure times 10^7), a fraction for a percentage;
    None for an empty or unreadable cell."""
    percent = "%" in text
    number = exact(text.replace("%", "").strip(), grouped=True)
    if number is None:
        return None
    if percent or label.endswith("%"):
        return number / HUNDRED, FRACTION
    if label.startswith("eps"):
        return number, INR_PER_SHARE
    if section == "ratios":
        return number, DAYS
    if label.startswith("no. of"):
        return number, COUNT
    return number * CRORE, INR


def _table_figures(section: str, table: _Table, basis: str) -> list[Figure]:
    dates = [column_date(text, key) for text, key in table.header[1:]]
    kind = SECTION_KIND[section]
    out = []
    for row in table.rows:
        if len(row) < 2 or not row[0]:
            continue
        label = screener_label(row[0])
        item = SCREENER_ITEMS.get((section, label))
        for end, text in zip(dates, row[1:], strict=False):
            got = cell_value(section, label, text) if end is not None else None
            if got is None or end is None:
                continue
            value, unit = got
            out.append(
                Figure(
                    section,
                    SCREENER_STATEMENT[section],
                    kind,
                    end,
                    row[0],
                    item,
                    value,
                    unit,
                    basis,
                )
            )
    return out


def parse_page(body: bytes, fetched_on: date) -> list[Figure]:
    """Every figure of one page; a period shown in two tables (shareholding's quarterly and
    yearly) is kept once, from the first. Shareholding and the summary take the basis the first
    section naming one names."""
    parser = _PageParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    named = (named_basis(parser.sections[s].sub) for s in SECTIONS if s in parser.sections)
    page_basis = next((b for b in named if b), "")
    out: list[Figure] = []
    for section in SECTIONS:
        found = parser.sections.get(section)
        if found is None or not found.tables:
            continue
        basis = named_basis(found.sub) or page_basis
        tables = found.tables if section == "shareholding" else found.tables[:1]
        seen: set[tuple[str, date]] = set()
        for table in tables:
            for figure in _table_figures(section, table, basis):
                key = (figure.label, figure.period_end)
                if key not in seen:
                    seen.add(key)
                    out.append(figure)
    for name, number in parser.top:
        label = screener_label(name)
        unit = TOP_UNITS.get(label)
        value = exact(number, grouped=True)
        if unit is None or value is None:
            continue
        if unit == INR:
            value *= CRORE
        elif unit == FRACTION:
            value /= HUNDRED
        out.append(
            Figure(
                TOP_RATIOS,
                SCREENER_STATEMENT[TOP_RATIOS],
                INSTANT,
                fetched_on,
                name,
                SCREENER_ITEMS.get((TOP_RATIOS, label)),
                value,
                unit,
                page_basis,
            )
        )
    return out
