"""The extract step: the raw results cache into `hat_fundamental_filing` and `hat_fundamental_fact`,
replacing what an earlier run wrote (both are projections of the cache, so a rerun gives the same
rows). Reads the cache only; writes only these two tables.

NSE, per covered instrument: the filings the results parse would read (`results.ingest`: the
cached listings of every symbol the instrument had, periods in [start, end], the basis rule), one
document once. A document not fetched, not 200 or unreadable is left out with its reason. Per
(period end, basis), documents take versions in filing order; one whose facts repeat the latest
version's adds nothing. `public_at` follows `results.timing` (the results announcement for a
first version, else the XBRL's own filing time).

The secondary source, per instrument: the consolidated and standalone pages of its current
symbol, one filing per (basis, period end) the page shows (the first page wins a key both show),
a standalone period kept only where neither source has consolidated figures for it.
`public_at`: the earliest NSE filing of the same period end (same basis first); else the
regulatory deadline at 23:59 IST (45 days after a quarter end, 60 for a March quarter or a year;
21 for a shareholding pattern alone), estimated, and never later than the page's fetch time. The
fetch-time summary (`top-ratios`) is public when fetched.
"""

from __future__ import annotations

import io
import logging
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from ...time import IST
from ..results.fetch import in_scope
from ..results.ingest import announcements_index, insurance_filings, member_filings
from ..results.nse import Announcement, Filing
from ..results.scope import BASIS_CONSOLIDATED_FIRST, CONSOLIDATED, by_rule
from ..results.secondary import page_url
from ..results.timing import (
    PublicAt,
    anchor_date,
    match_announcement,
    public_at,
    xbrl_at,
)
from ..results.xbrl import XbrlError
from .items import ANNUAL, SHAREHOLDING, SNAPSHOT
from .nse import Document, fact_key, parse_document
from .screener import Figure, parse_page

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..results.raw import RawCache
    from ..results.universe import Member

logger = logging.getLogger(__name__)

NSE = "nse"
SCREENER = "screener"
PAGE_FORMAT = "page"
SHAREHOLDING_DAYS = 21  # SEBI LODR Reg 31: the pattern within 21 days of the quarter end
QUARTER_DAYS = 45
YEAR_DAYS = 60
DEADLINE_TIME = time(23, 59)


@dataclass(frozen=True)
class Row:
    """One fact as stored (`HatFundamentalFact` without its filing)."""

    statement: str
    period_kind: str
    period_start: date | None
    period_end: date
    element: str
    context: str
    item: str | None
    value: Decimal
    unit: str


@dataclass
class FilingOut:
    """One filing as stored, with its facts."""

    period_end: date
    basis: str
    source: str
    format: str
    family: str
    version: int
    public_at: PublicAt
    fetched_at: datetime
    source_url: str
    sha256: str
    rows: list[Row]
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass
class ExtractReport:
    members: int = 0
    nse_filings: int = 0
    screener_filings: int = 0
    facts: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)

    def __str__(self) -> str:
        return (
            f"members={self.members} nse_filings={self.nse_filings} "
            f"screener_filings={self.screener_filings} facts={self.facts} "
            f"outcomes={dict(sorted(self.outcomes.items()))}"
        )


# --- NSE --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    filing: Filing
    document: Document
    basis: str
    sha256: str
    fetched_at: datetime


def nse_candidates(
    cache: RawCache, filings: Sequence[Filing], outcomes: Counter[str]
) -> list[_Candidate]:
    """The readable documents of `filings`, one per sha256, each with its basis (the document's,
    else the listing's)."""
    out = []
    seen: set[str] = set()
    for filing in filings:
        entry = cache.entry(filing.xbrl_url or "")
        if entry is None or entry.status != 200:
            outcomes["nse_not_fetched" if entry is None else f"nse_http_{entry.status}"] += 1
            continue
        if entry.sha256 in seen:
            continue
        seen.add(entry.sha256)
        try:
            document = parse_document(cache.body(entry))
        except XbrlError as exc:
            outcomes[f"nse_{exc}"] += 1
            continue
        basis = document.basis or filing.basis
        if not basis:
            outcomes["nse_basis_unknown"] += 1
            continue
        out.append(_Candidate(filing, document, basis, entry.sha256, entry.fetched_at))
    return out


def _filed_order(c: _Candidate) -> tuple[Any, ...]:
    filed = xbrl_at(c.filing)
    return (
        c.document.period_end,
        c.basis,
        filed is None,
        filed.at.isoformat() if filed else "",
        c.filing.seq,
    )


def _announced_at(c: _Candidate, announcements: Sequence[Announcement]) -> datetime | None:
    anchor = anchor_date(c.filing)
    match = match_announcement(announcements, anchor, c.document.period_end) if anchor else None
    return match.public if match else None


def nse_versions(
    candidates: Sequence[_Candidate],
    announcements: Sequence[Announcement],
    outcomes: Counter[str],
) -> list[FilingOut]:
    """Versions per (period end, basis) in filing order, a repeat of the latest version left
    out, each with its `public_at` (`results.timing`)."""
    out = []
    latest: dict[tuple[date, str], tuple[int, frozenset[Any]]] = {}
    for c in sorted(candidates, key=_filed_order):
        key = (c.document.period_end, c.basis)
        said = fact_key(c.document.facts)
        held = latest.get(key)
        if held is not None and held[1] == said:
            outcomes["nse_same_facts"] += 1
            continue
        version = 1 if held is None else held[0] + 1
        filed = xbrl_at(c.filing)
        announced = _announced_at(c, announcements) if version == 1 else None
        when = public_at(version, announced, filed)
        if when is None:
            outcomes["nse_no_time"] += 1
            continue
        latest[key] = (version, said)
        outcomes["nse_stored"] += 1
        out.append(
            FilingOut(
                key[0],
                c.basis,
                NSE,
                c.document.kind.format,
                c.document.family,
                version,
                when,
                c.fetched_at,
                c.filing.xbrl_url or "",
                c.sha256,
                [Row(**vars(fact)) for fact in c.document.facts],
            )
        )
    return out


# --- the secondary source ---------------------------------------------------------------------


def deadline_at(period_end: date, days: int) -> datetime:
    return datetime.combine(period_end + timedelta(days=days), DEADLINE_TIME, tzinfo=IST)


def screener_public_at(
    figures: Sequence[Figure], nse: dict[date, list[FilingOut]], basis: str, fetched_at: datetime
) -> PublicAt:
    """When one page period's figures became public (module doc)."""
    end = figures[0].period_end
    if all(f.statement == SNAPSHOT for f in figures):
        return PublicAt(fetched_at, "observed", False)
    filed = nse.get(end, [])
    if filed:
        same = [f for f in filed if f.basis == basis] or filed
        first = min(same, key=lambda f: f.public_at.at).public_at
        return PublicAt(first.at, first.source, first.estimated)
    if all(f.statement == SHAREHOLDING for f in figures):
        days = SHAREHOLDING_DAYS
    elif end.month == 3 or any(f.period_kind == ANNUAL for f in figures):
        days = YEAR_DAYS
    else:
        days = QUARTER_DAYS
    due = deadline_at(end, days)
    if fetched_at < due:
        return PublicAt(fetched_at, "observed", True)
    return PublicAt(due, "deadline", True)


def screener_filings(
    pages: Iterable[tuple[str, str, datetime, list[Figure]]],
    nse: Sequence[FilingOut],
    nse_consolidated: Iterable[date],
    rule: str = BASIS_CONSOLIDATED_FIRST,
) -> list[FilingOut]:
    """One filing per (basis, period end) of the pages (url, sha256, fetched_at, figures), the
    first page winning a key two pages show; under the basis rule, a standalone period only
    where no consolidated figures exist (here or in `nse_consolidated`)."""
    groups: dict[tuple[str, date], tuple[str, str, datetime, list[Figure]]] = {}
    for url, sha, fetched_at, figures in pages:
        by_key: dict[tuple[str, date], list[Figure]] = defaultdict(list)
        for figure in figures:
            if figure.basis:
                by_key[(figure.basis, figure.period_end)].append(figure)
        for key, found in by_key.items():
            groups.setdefault(key, (url, sha, fetched_at, found))
    nse_by_end: dict[date, list[FilingOut]] = defaultdict(list)
    for filing in nse:
        nse_by_end[filing.period_end].append(filing)
    kept = by_rule([_Key(end, basis) for basis, end in groups.keys()], rule, nse_consolidated)
    out = []
    for key in sorted(kept, key=lambda k: (k.period_end, k.basis)):
        url, sha, fetched_at, found = groups[(key.basis, key.period_end)]
        out.append(
            FilingOut(
                key.period_end,
                key.basis,
                SCREENER,
                PAGE_FORMAT,
                SCREENER,
                1,
                screener_public_at(found, nse_by_end, key.basis, fetched_at),
                fetched_at,
                url,
                sha,
                [
                    Row(
                        f.statement,
                        f.period_kind,
                        None,
                        f.period_end,
                        f.label,
                        f.section,
                        f.item,
                        f.value,
                        f.unit,
                    )
                    for f in found
                ],
            )
        )
    return out


class Cursor(Protocol):
    """The DB-API cursor calls the step makes (psycopg2's)."""

    def execute(self, query: str, params: Sequence[object] = ...) -> object: ...

    def copy_expert(self, sql: str, file: io.StringIO) -> object: ...


@dataclass(frozen=True)
class _Key:
    period_end: date
    basis: str


# --- the step ---------------------------------------------------------------------------------

FILING_COLUMNS = (
    "id",
    "instrument_id",
    "symbol",
    "isin",
    "period_end",
    "basis",
    "source",
    "format",
    "family",
    "version",
    "public_at",
    "public_at_source",
    "public_at_estimated",
    "point_in_time",
    "fetched_at",
    "source_url",
    "sha256",
    "facts",
    "recorded_at",
)
FACT_COLUMNS = (
    "filing_id",
    "n",
    "statement",
    "period_kind",
    "period_start",
    "period_end",
    "element",
    "context",
    "item",
    "value",
    "unit",
)


def copy_text(value: object) -> str:
    """One value in Postgres COPY text format."""
    if value is None:
        return "\\N"
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value)
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def copy_lines(rows: Iterable[Sequence[object]]) -> str:
    return "".join("\t".join(copy_text(v) for v in row) + "\n" for row in rows)


def filing_values(member: Member, filing: FilingOut, now: datetime) -> tuple[object, ...]:
    return (
        filing.id,
        member.instrument_id,
        member.symbols[0],
        member.isin,
        filing.period_end,
        filing.basis,
        filing.source,
        filing.format,
        filing.family,
        filing.version,
        filing.public_at.at,
        filing.public_at.source,
        filing.public_at.estimated,
        filing.source == NSE,
        filing.fetched_at,
        filing.source_url,
        filing.sha256,
        len(filing.rows),
        now,
    )


def fact_values(filing: FilingOut) -> Iterable[tuple[object, ...]]:
    for n, row in enumerate(filing.rows, 1):
        yield (
            filing.id,
            n,
            row.statement,
            row.period_kind,
            row.period_start,
            row.period_end,
            row.element,
            row.context,
            row.item,
            row.value,
            row.unit,
        )


def member_filings_out(
    nse_cache: RawCache,
    secondary: RawCache,
    member: Member,
    context: tuple[dict[str, list[Announcement]], list[Filing], dict[str, str]],
    span: tuple[date, date],
    rule: str,
    outcomes: Counter[str],
) -> list[FilingOut]:  # pragma: no cover - the cache
    announced, insurance, owners = context
    listed = [
        f
        for f in member_filings(nse_cache, member, insurance, owners)
        if in_scope(f, span[0], span[1])
    ]
    filings = by_rule(listed, rule)
    items = sorted((a for s in member.symbols for a in announced.get(s, [])), key=lambda a: a.at)
    nse = nse_versions(nse_candidates(nse_cache, filings, outcomes), items, outcomes)
    consolidated = {f.period_end for f in filings if f.basis == CONSOLIDATED}
    consolidated |= {f.period_end for f in nse if f.basis == CONSOLIDATED}
    pages = []
    for basis in (CONSOLIDATED, "standalone"):
        url = page_url(member.symbols[0], basis)
        entry = secondary.entry(url)
        if entry is None or entry.status != 200:
            continue
        figures = parse_page(secondary.body(entry), entry.fetched_at.astimezone(IST).date())
        pages.append((url, entry.sha256, entry.fetched_at, figures))
    if not pages:
        outcomes["screener_no_page"] += 1
    return nse + screener_filings(pages, nse, consolidated, rule)


def extract(
    nse_cache: RawCache,
    secondary: RawCache,
    members: Sequence[Member],
    span: tuple[date, date],
    rule: str = BASIS_CONSOLIDATED_FIRST,
    *,
    replace_all: bool = True,
) -> ExtractReport:  # pragma: no cover - the database and the cache
    from app.database import db_session_scope, live_writer_engine

    from ...models import HatFundamentalFact, HatFundamentalFiling
    from ...time import utc_now

    for model in (HatFundamentalFiling, HatFundamentalFact):
        model.__table__.create(live_writer_engine, checkfirst=True)
    context = (
        announcements_index(nse_cache),
        insurance_filings(nse_cache),
        {m.isin: m.instrument_id for m in members},
    )
    report = ExtractReport()
    now = utc_now()
    with db_session_scope(writes_data=True) as session:
        cursor = raw_cursor(session, HatFundamentalFiling)
        _clear(cursor, members, replace_all=replace_all)
        for n, member in enumerate(members, 1):
            filings = member_filings_out(
                nse_cache, secondary, member, context, span, rule, report.outcomes
            )
            copy_rows(
                cursor,
                "hat_fundamental_filing",
                FILING_COLUMNS,
                (filing_values(member, f, now) for f in filings),
            )
            copy_rows(
                cursor,
                "hat_fundamental_fact",
                FACT_COLUMNS,
                (v for f in filings for v in fact_values(f)),
            )
            report.members = n
            report.nse_filings += sum(1 for f in filings if f.source == NSE)
            report.screener_filings += sum(1 for f in filings if f.source == SCREENER)
            report.facts += sum(len(f.rows) for f in filings)
            if n % 50 == 0:
                logger.info("extract %d/%d members, %d facts", n, len(members), report.facts)
    return report


def raw_cursor(session: Session, model: type) -> Cursor:  # pragma: no cover - the database
    connection = session.connection(bind_arguments={"mapper": model})
    return connection.connection.driver_connection.cursor()


def _clear(
    cursor: Cursor, members: Sequence[Member], *, replace_all: bool
) -> None:  # pragma: no cover - the database
    """Every earlier row (`replace_all`), else every earlier row of these members, under a lock
    so two runs do not interleave."""
    cursor.execute("LOCK TABLE hat_fundamental_filing, hat_fundamental_fact IN EXCLUSIVE MODE")
    if replace_all:
        cursor.execute("TRUNCATE hat_fundamental_fact, hat_fundamental_filing")
        return
    ids = [m.instrument_id for m in members]
    cursor.execute(
        "DELETE FROM hat_fundamental_fact WHERE filing_id IN (SELECT id FROM "
        "hat_fundamental_filing WHERE instrument_id = ANY(%s::uuid[]))",
        (ids,),
    )
    cursor.execute(
        "DELETE FROM hat_fundamental_filing WHERE instrument_id = ANY(%s::uuid[])", (ids,)
    )


def copy_rows(
    cursor: Cursor, table: str, columns: Sequence[str], rows: Iterable[Sequence[object]]
) -> None:  # pragma: no cover
    text = copy_lines(rows)
    if text:
        cursor.copy_expert(f"COPY {table} ({', '.join(columns)}) FROM STDIN", io.StringIO(text))
