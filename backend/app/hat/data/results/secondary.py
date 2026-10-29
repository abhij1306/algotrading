"""The secondary source of quarterly results: the public company pages of screener.in, read for
the quarters NSE does not give (a company-quarter missing from NSE's listing, or whose XBRL fails)
and to cross-check the quarters NSE gives.

Owner rules (2026-10-28): only pages that need no login (never log in, never send credentials),
as `robots.txt` allows (it disallows `/company/source/quarter/*`, which is never asked); one
request every 3 s; raw pages cached (`raw.RawCache`). Each company has two pages: the
consolidated figures at `/company/<SYMBOL>/consolidated/` and the standalone ones at
`/company/<SYMBOL>/`. A page's "Quarterly Results" table lists the latest 13 quarters (Sales, or
Revenue for banks, Net Profit and EPS, in crore rupees, EPS in rupees) and names its basis above
the table; a company without consolidated accounts gets its standalone figures on both pages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from urllib.parse import quote

from ...money import exact
from .raw import RawCache
from .scope import BASIS_CONSOLIDATED_FIRST, read_standalone
from .universe import Member

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .nse import Answer

logger = logging.getLogger(__name__)

BASE = "https://www.screener.in"
INTERVAL_SECONDS = 3.0
# a page is asked again a week after its fetch, or sooner once NSE lists a filing of the company
# broadcast after it (`page_reusable`): the page changes only with a new quarter
PAGE_MAX_AGE = timedelta(days=7)
CRORE = Decimal(10) ** 7
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)",
    "Accept": "text/html,application/xhtml+xml",
}
REVENUE_LABELS = ("sales", "revenue")
PROFIT_LABELS = ("net profit",)
EPS_LABELS = ("eps in rs",)


def page_url(symbol: str, basis: str) -> str:
    path = f"/company/{quote(symbol, safe='')}/"
    return BASE + (path + "consolidated/" if basis == "consolidated" else path)


@dataclass(frozen=True)
class Quarter:
    period_end: date
    basis: str
    revenue: Decimal | None  # rupees
    net_profit: Decimal | None  # rupees
    eps_basic: Decimal | None  # rupees a share


class _QuartersTable(HTMLParser):
    """Collects the text of `<section id="quarters">`: the basis line, each header cell's
    `data-date-key` and each body row's cells."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0  # section nesting while inside the quarters section
        self.in_sub = False
        self.sub = ""
        self.dates: list[str] = []
        self.rows: list[list[str]] = []
        self.in_table = False
        self.in_body = False
        self.cell: list[str] | None = None
        self.row: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "section":
            if self.depth or values.get("id") == "quarters":
                self.depth += 1
            return
        if not self.depth:
            return
        if tag == "p" and "sub" in (values.get("class") or "").split() and not self.sub:
            self.in_sub = True
        elif tag == "table" and not self.dates and not self.rows:
            self.in_table = True
        elif self.in_table:
            self._table_start(tag, values.get("data-date-key"))

    def _table_start(self, tag: str, date_key: str | None) -> None:
        if tag == "th" and date_key:
            self.dates.append(date_key)
        elif tag == "tbody":
            self.in_body = True
        elif self.in_body and tag == "tr":
            self.row = []
        elif self.row is not None and tag == "td":
            self.cell = []

    def handle_endtag(self, tag: str) -> None:
        if not self.depth:
            return
        if tag == "section":
            self.depth -= 1
        elif tag == "p" and self.in_sub:
            self.in_sub = False
        elif tag == "td" and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == "tbody":
            self.in_body = False
            self.in_table = False

    def handle_data(self, data: str) -> None:
        if self.in_sub:
            self.sub += data
        if self.cell is not None:
            self.cell.append(data)


def _number(text: str) -> Decimal | None:
    return exact(text.replace("%", ""), grouped=True)


def _row(rows: list[list[str]], labels: tuple[str, ...]) -> list[str] | None:
    for row in rows:
        label = row[0].replace("+", "").strip().lower() if row else ""
        if label in labels:
            return row[1:]
    return None


def named_basis(label: str) -> str:
    """The basis a page names first above its table ('Standalone Figures in Rs. Crores / View
    Consolidated' is standalone: the link names the other basis); "" when it names neither."""
    named = [(label.lower().find(b), b) for b in ("consolidated", "standalone")]
    found = sorted((at, b) for at, b in named if at >= 0)
    return found[0][1] if found else ""


def parse_page(body: bytes) -> list[Quarter]:
    """The quarters of one company page, with the basis the page names (none when it names
    neither)."""
    table = _QuartersTable()
    table.feed(body.decode("utf-8", errors="replace"))
    basis = named_basis(table.sub)
    if not basis or not table.dates:
        return []
    revenue = _row(table.rows, REVENUE_LABELS)
    profit = _row(table.rows, PROFIT_LABELS)
    eps = _row(table.rows, EPS_LABELS)

    def cell(values: list[str] | None, index: int, scale: Decimal) -> Decimal | None:
        if values is None or index >= len(values):
            return None
        value = _number(values[index])
        return None if value is None else value * scale

    out = []
    for index, key in enumerate(table.dates):
        try:
            end = date.fromisoformat(key)
        except ValueError:
            continue
        out.append(
            Quarter(
                end,
                basis,
                cell(revenue, index, CRORE),
                cell(profit, index, CRORE),
                cell(eps, index, Decimal(1)),
            )
        )
    return out


def page_reusable(fetched_at: datetime, now: datetime, latest_filing: datetime | None) -> bool:
    """Whether a cached page stands: fetched within `PAGE_MAX_AGE` of `now` and not before the
    company's latest NSE filing (None: none known)."""
    return fetched_at >= now - PAGE_MAX_AGE and (
        latest_filing is None or fetched_at >= latest_filing
    )


def latest_filings(nse: RawCache, members: Iterable[Member]) -> dict[str, datetime]:
    """Each member's latest NSE results filing time (broadcast, else document creation), from
    the cached NSE listings, keyed by its current symbol; members with none are left out."""
    from .fetch import listing_urls
    from .nse import filings_of

    entries = nse.entries()
    out: dict[str, datetime] = {}
    for member in members:
        answers = [
            (listing, nse.body(entry))
            for symbol in member.symbols
            for listing, url in listing_urls(symbol)
            if (entry := entries.get(url)) is not None and entry.status == 200
        ]
        times = [
            t for f in filings_of(answers) if (t := f.broadcast_at or f.created_at) is not None
        ]
        if times:
            out[member.symbols[0]] = max(times)
    return out


@dataclass
class SecondaryReport:
    members: int = 0
    pages: int = 0
    fetched: int = 0
    statuses: dict[int, int] | None = None


def fetch_secondary(
    cache: RawCache,
    members: list[Member],
    basis: str = BASIS_CONSOLIDATED_FIRST,
    nse: RawCache | None = None,
) -> SecondaryReport:  # pragma: no cover - the network
    """The consolidated page of every covered instrument's current symbol, and its standalone
    page when the basis rule asks for it (`scope.read_standalone`), one request every 3 s; a
    cached page is asked again only when `page_reusable` says so against the company's latest
    filing in the NSE cache `nse` (run `fetch` first)."""
    from .nse import Client

    client = Client(cache, interval=INTERVAL_SECONDS, headers=HEADERS)
    now = datetime.now(UTC)
    latest = latest_filings(nse, members) if nse is not None else {}
    report = SecondaryReport()

    def read(url: str, filed: datetime | None) -> Answer:
        before = client.requests
        answer = client.get(
            url, referer=None, reuse=lambda e: page_reusable(e.fetched_at, now, filed)
        )
        report.pages += 1
        report.fetched += client.requests > before
        return answer

    for n, member in enumerate(members, 1):
        symbol = member.symbols[0]
        filed = latest.get(symbol)
        consolidated = read(page_url(symbol, "consolidated"), filed)
        found = len(parse_page(consolidated.body)) if consolidated.status == 200 else None
        if read_standalone(basis, found):
            read(page_url(symbol, "standalone"), filed)
        report.members = n
        if n % 50 == 0:
            logger.info("secondary %d/%d, %d requests", n, len(members), client.requests)
    report.statuses = dict(client.statuses)
    return report
