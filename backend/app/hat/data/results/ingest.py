"""The parse step: the cached listings, documents and announcements into `hat_financial_result`,
append-only; then the secondary source's quarters for what NSE does not give.

Per covered instrument, every XBRL its listings give for a period ending in [start, end] (one
document once, by URL; a listing row whose ISIN names another covered instrument is left to it):

- a document already stored (same sha256 for the instrument) is skipped, so a rerun adds only
  what is new; a document not fetched, not 200 or unreadable is excluded with its reason;
- the period and basis are the document's (the listing's basis where the document states none);
  a period that differs from the listing's is kept and flagged (`period_mismatch`), except one
  exactly a year before the listing's (a document misdated by a year: it would become a false
  revision of that earlier quarter), which is excluded (`period_year_before_listing`);
- the parser's flags (`xbrl.FLAG_*`) are kept on the row (`quality_flags`);
- an insurer (the insurance listing, or an insurance taxonomy) is a metadata row,
  `format=insurance_skipped`, without figures;
- a Q4 document with only the year's figures gets Q4 = year - nine months (the document's nine
  months, else the stored Q3 row's year to date on the same basis), `q4_derived`;
- per (period_end, basis), new filings take the next versions in filing order, each superseding
  the one before; a filing whose figures equal the latest version's adds nothing;
- `public_at` and `xbrl_at` follow `timing.py`.

Then, per instrument, each quarter the secondary source shows on a basis for which NSE has no row
becomes a `source=secondary` row (`public_at` the quarter's NSE results announcement when one is
found, else the regulatory deadline, `public_at_estimated`; a quarter that ended before the company
listed counts from the results that first carry it, `timing.comparative_quarter`). Every filing
considered is written to `<report_dir>/filings.csv` with its outcome, member by member as each
member's rows commit.

Last, every stored row's `public_at` is worked out again under today's rules and the cache
(`MemberIngest.retime`); one that comes out later is moved later, one that comes out earlier only
onto an announcement's dissemination time after the quarter's end and by at most 120 days (the
only timing changes the table allows), and each move is written to
`<report_dir>/retimed-<UTC time>.csv` (one file per run, kept; written as each member commits,
so a run that stops partway still logs every committed move).
"""

from __future__ import annotations

import csv
import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ...models import HatFinancialResult, HatInstrument
from ...time import IST, utc_now
from .fetch import in_scope, listing_urls
from .growth import derive_q4, quarter_ends_before, year_ago
from .nse import (
    ANNOUNCEMENTS_API,
    LISTING_INSURANCE,
    RESULTS_API,
    Announcement,
    Filing,
    filings_of,
    parse_announcements,
)
from .raw import RawCache
from .scope import BASIS_CONSOLIDATED_FIRST, CONSOLIDATED, by_rule
from .secondary import Quarter, page_url, parse_page
from .timing import (
    FiledAt,
    PublicAt,
    anchor_date,
    comparative_quarter,
    deadline,
    end_of,
    match_announcement,
    public_at,
    quarter_announcement,
    results_announcements,
    unbroadcast,
    xbrl_at,
)
from .universe import Member
from .xbrl import Figures, Parsed, XbrlError, quarter_start
from .xbrl import parse as parse_xbrl

logger = logging.getLogger(__name__)

# a secondary-source quarter: (figures, page URL, page sha256, when the page was fetched)
PageQuarter = tuple[Quarter, str, str, datetime]

SOURCE_NSE = "nse"
SOURCE_SECONDARY = "secondary"
FORMAT_INSURANCE = "insurance_skipped"
FORMAT_SECONDARY = "secondary"


@dataclass
class Outcome:
    instrument_id: str
    symbol: str
    listing: str
    seq: str
    listing_period_end: str
    period_end: str
    basis: str
    format: str
    url: str
    result: str  # stored | already_stored | same_figures | insurance_skipped | excluded
    reason: str = ""
    version: int = 0
    q4_derived: bool = False
    period_mismatch: bool = False
    public_at_source: str = ""
    flags: str = ""


@dataclass
class ParseReport:
    members: int = 0
    stored: int = 0
    secondary: int = 0
    retimed: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)


@dataclass
class Retimed:
    """One stored row whose `public_at` moved (`MemberIngest.retime`); `lead_s` < 0: earlier."""

    id: str
    symbol: str
    period_end: str
    basis: str
    source: str
    version: int
    old_at: str
    old_source: str
    new_at: str
    new_source: str
    lead_s: int
    old_estimated: bool
    new_estimated: bool
    old_xbrl_at: str
    old_xbrl_estimated: bool | None
    new_xbrl_at: str
    new_xbrl_estimated: bool | None


@dataclass(frozen=True)
class Candidate:
    filing: Filing
    parsed: Parsed | None  # None for an insurance-listing row
    basis: str
    period_end: date
    sha256: str
    raw_path: str
    filed: FiledAt | None


@dataclass
class Held:
    """The latest stored version of one (period_end, basis, source)."""

    id: uuid.UUID
    version: int
    figures: tuple[Any, ...]
    ytd_start: date | None
    ytd: Figures | None
    public_at: datetime
    public_at_source: str


# --- reading the cache ----------------------------------------------------------------------


def announcements_index(cache: RawCache) -> dict[str, list[Announcement]]:
    """Every cached announcement day's results announcements, by symbol, earliest first."""
    items: list[Announcement] = []
    for url, entry in cache.entries().items():
        if url.startswith(ANNOUNCEMENTS_API) and entry.status == 200:
            items += parse_announcements(cache.body(entry))
    return results_announcements(items)


def insurance_filings(cache: RawCache) -> list[Filing]:
    """The insurers' filings of the latest cached insurance listing."""
    found = [
        e
        for url, e in cache.entries().items()
        if url.startswith(RESULTS_API) and "index=insurance" in url and e.status == 200
    ]
    if not found:
        return []
    latest = max(found, key=lambda e: e.fetched_at)
    return filings_of([(LISTING_INSURANCE, cache.body(latest))])


def member_filings(
    cache: RawCache, member: Member, insurance: Sequence[Filing], owners: dict[str, str]
) -> list[Filing]:
    """The member's filings from the cached listings of each symbol it had, one per XBRL URL
    (earliest broadcast first); a row that names the ISIN of another company (`owners`: each
    known ISIN -> its company's instrument, a chain's earlier ISINs -> its latest) is left out.
    An ISIN no instrument holds (NSE lists some companies under an ISIN from before 2016) is
    kept: the symbol's own listing carries it."""
    answers = []
    for symbol in member.symbols:
        for listing, url in listing_urls(symbol):
            entry = cache.entry(url)
            if entry is not None and entry.status == 200:
                answers.append((listing, cache.body(entry)))
    filings = filings_of(answers) + [f for f in insurance if f.symbol in member.symbols]
    out: dict[str, Filing] = {}
    for filing in sorted(filings, key=_filing_order):
        if filing.isin and owners.get(filing.isin, member.instrument_id) != member.instrument_id:
            continue
        if filing.xbrl_url and filing.xbrl_url not in out:
            out[filing.xbrl_url] = filing
    return list(out.values())


def _filing_order(filing: Filing) -> tuple[Any, ...]:
    when = filing.created_at or filing.broadcast_at
    return (when is None, when or datetime.min, filing.seq)


# --- the step -------------------------------------------------------------------------------


def parse(
    nse: RawCache,
    secondary: RawCache,
    members: Sequence[Member],
    start: date,
    end: date,
    report_dir: Path,
    basis: str = BASIS_CONSOLIDATED_FIRST,
) -> ParseReport:  # pragma: no cover - the database and the cache
    from app.database import db_session_scope, live_writer_engine

    from ...models import HatFinancialGrowth
    from ...schema import ensure_live_schema

    HatFinancialResult.__table__.create(live_writer_engine, checkfirst=True)
    HatFinancialGrowth.__table__.create(live_writer_engine, checkfirst=True)
    ensure_live_schema(live_writer_engine)
    announced = announcements_index(nse)
    insurance = insurance_filings(nse)
    with db_session_scope() as session:
        listed = first_listed(session)
        owners = chain_owners(session, members)
    report = ParseReport()
    outcomes: list[Outcome] = []
    retimed: list[Retimed] = []
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    with (
        RowLog(report_dir / "filings.csv", Outcome) as outcome_log,
        RowLog(report_dir / f"retimed-{stamp}.csv", Retimed) as retimed_log,
    ):
        for n, member in enumerate(members, 1):
            every = member_filings(nse, member, insurance, owners)
            filings = by_rule([f for f in every if in_scope(f, start, end)], basis)
            with db_session_scope(writes_data=True) as session:
                ingest = MemberIngest(
                    session, nse, member, announced, listed.get(member.instrument_id)
                )
                done = ingest.nse(filings)
                if not is_insurer(member, insurance, done):  # insurers: metadata rows only
                    quarters = secondary_by_rule(
                        _secondary_quarters(secondary, member),
                        basis,
                        ingest.nse_consolidated() | filed_consolidated(filings),
                    )
                    done += ingest.secondary(quarters, start, end)
                moved = ingest.retime(every)
            # the member's rows are committed: log them now, so a later crash loses no record
            outcome_log.add(done)
            retimed_log.add(moved)
            outcomes += done
            retimed += moved
            report.members = n
            if n % 50 == 0:
                logger.info("parse %d/%d members, %d outcomes", n, len(members), len(outcomes))
    for item in outcomes:
        report.outcomes[item.result] = report.outcomes.get(item.result, 0) + 1
    report.stored = sum(
        1 for o in outcomes if o.result == "stored" and o.format != FORMAT_SECONDARY
    )
    report.secondary = sum(
        1 for o in outcomes if o.format == FORMAT_SECONDARY and o.result == "stored"
    )
    report.retimed = len(retimed)
    return report


_FIRST_LISTED_SQL = """
WITH RECURSIVE chain(id, head) AS (
  SELECT id, id FROM hat_instrument
  UNION
  SELECT l.old_instrument_id, c.head FROM hat_isin_lineage l
  JOIN chain c ON l.new_instrument_id = c.id WHERE l.status = 'linked'
)
SELECT CAST(c.head AS TEXT), min(i.listed_on) FROM chain c
JOIN hat_instrument i ON i.id = c.id GROUP BY c.head
"""


def chain_owners(
    session: Session, members: Sequence[Member]
) -> dict[str, str]:  # pragma: no cover - the database
    """Each known ISIN -> its instrument, and each covered ISIN, an earlier one of a chain too,
    -> its company's (latest) instrument."""
    known = session.execute(select(HatInstrument.isin, HatInstrument.id)).tuples()
    owners = {isin: str(instrument_id) for isin, instrument_id in known}
    owners |= {m.isin: m.instrument_id for m in members}
    return owners | {isin: m.instrument_id for m in members for isin in _isins(session, m.older)}


def _isins(session: Session, ids: Sequence[str]) -> list[str]:  # pragma: no cover - the database
    if not ids:
        return []
    return list(
        session.scalars(
            select(HatInstrument.isin).where(HatInstrument.id.in_([uuid.UUID(i) for i in ids]))
        )
    )


def first_listed(session: Session) -> dict[str, date | None]:  # pragma: no cover - the database
    """Each instrument's listing day: the earliest of its own and its linked earlier ISINs'
    (`hat_isin_lineage`), so an ISIN change is not a new listing."""
    found = session.execute(text(_FIRST_LISTED_SQL), bind_arguments={"mapper": HatInstrument})
    return dict(found.tuples().all())


def _secondary_quarters(cache: RawCache, member: Member) -> list[PageQuarter]:
    out = []
    for basis in ("consolidated", "standalone"):
        url = page_url(member.symbols[0], basis)
        entry = cache.entry(url)
        if entry is not None and entry.status == 200:
            out += [(q, url, entry.sha256, entry.fetched_at) for q in parse_page(cache.body(entry))]
    return out


def filed_consolidated(filings: Iterable[Filing]) -> set[date]:
    """The period ends NSE lists a consolidated filing for, stored or not: a quarter whose
    consolidated XBRL is unreadable or excluded still has consolidated results, so the secondary
    source's standalone figures must not stand in for it."""
    return {f.period_end for f in filings if f.basis == CONSOLIDATED}


def secondary_by_rule(
    quarters: Sequence[PageQuarter], rule: str, consolidated_elsewhere: Iterable[date]
) -> list[PageQuarter]:
    """The secondary-source quarters the basis rule keeps (`scope.by_rule`), a quarter NSE gives
    consolidated figures for counting as consolidated."""
    kept = {id(q) for q in by_rule([t[0] for t in quarters], rule, consolidated_elsewhere)}
    return [t for t in quarters if id(t[0]) in kept]


def is_insurer(member: Member, insurance: Sequence[Filing], outcomes: Sequence[Outcome]) -> bool:
    """Whether the member files as an insurer: it is in the insurance listing, or a filing of its
    reads as an insurer's."""
    return any(f.symbol in member.symbols for f in insurance) or any(
        o.format == FORMAT_INSURANCE for o in outcomes
    )


def document_period(document_end: date | None, listing_end: date) -> tuple[date, bool]:
    """A2 rule 3: the period end is the document's when it states one (else the listing's), and
    whether it differs from the listing's (kept, flagged and logged)."""
    end = document_end or listing_end
    return end, end != listing_end


def year_before_listing(document_end: date | None, listing_end: date) -> bool:
    """Whether the document's period end is exactly a year before the listing's (D2: misdated by
    a year, excluded rather than written as a revision of the earlier quarter)."""
    return document_end is not None and document_end == year_ago(listing_end)


class RowLog:
    """A CSV of dataclass rows of `kind`, written as rows arrive (each flushed at once)."""

    def __init__(self, path: Path, kind: type) -> None:
        self.path = path
        self.fields = list(kind.__dataclass_fields__)

    def __enter__(self) -> RowLog:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.handle, fieldnames=self.fields)
        self.writer.writeheader()
        return self

    def add(self, items: Iterable[Any]) -> None:
        self.writer.writerows(asdict(i) for i in items)
        self.handle.flush()

    def __exit__(self, *_: object) -> None:
        self.handle.close()


def figures_key(figures: Figures | None) -> tuple[Any, ...]:
    if figures is None:
        return (None,) * 5
    return (
        figures.revenue,
        figures.total_income,
        figures.net_profit,
        figures.eps_basic,
        figures.eps_diluted,
    )


def _dec(value: Decimal | None) -> Decimal | None:
    return None if value is None else Decimal(value).normalize()


class MemberIngest:  # pragma: no cover - the database
    """One instrument's parse: what is stored, then what is new."""

    def __init__(
        self,
        session: Session,
        cache: RawCache,
        member: Member,
        announced: dict[str, list[Announcement]],
        listed_on: date | None = None,
    ) -> None:
        self.session = session
        self.cache = cache
        self.member = member
        self.listed_on = listed_on
        self.items = sorted(
            (a for s in member.symbols for a in announced.get(s, [])), key=lambda a: a.at
        )
        self.latest: dict[tuple[date, str, str], Held] = {}
        self.shas: set[str] = set()
        self.rows: list[HatFinancialResult] = list(
            session.scalars(
                select(HatFinancialResult)
                .where(HatFinancialResult.instrument_id == uuid.UUID(member.instrument_id))
                .order_by(HatFinancialResult.version)
            )
        )
        # the rows its earlier ISINs' instruments hold: read for the trades of those years, so
        # they are re-timed too (`retime`), never extended; a document or quarter they hold is
        # held by the company, so it is never stored again under the latest instrument
        self.older_rows: list[HatFinancialResult] = (
            list(
                session.scalars(
                    select(HatFinancialResult)
                    .where(
                        HatFinancialResult.instrument_id.in_([uuid.UUID(i) for i in member.older])
                    )
                    .order_by(HatFinancialResult.version)
                )
            )
            if member.older
            else []
        )
        for row in [*self.older_rows, *self.rows]:  # its own rows win a quarter both hold
            self.shas.add(row.sha256)
            self._hold(row)

    def nse_consolidated(self) -> set[date]:
        """The period ends with a stored consolidated NSE row."""
        return {e for e, b, source in self.latest if b == "consolidated" and source == SOURCE_NSE}

    def _hold(self, row: HatFinancialResult) -> None:
        ytd = Figures(
            _dec(row.ytd_revenue), None, _dec(row.ytd_net_profit), _dec(row.ytd_eps), None
        )
        quarter = Figures(
            _dec(row.revenue),
            _dec(row.total_income),
            _dec(row.net_profit),
            _dec(row.eps_basic),
            _dec(row.eps_diluted),
        )
        self.latest[(row.period_end, row.basis, row.source)] = Held(
            row.id,
            row.version,
            figures_key(quarter),
            row.ytd_period_start,
            ytd,
            row.public_at,
            row.public_at_source,
        )

    def _outcome(self, filing: Filing, **values: str | int | bool) -> Outcome:
        return Outcome(
            instrument_id=self.member.instrument_id,
            symbol=filing.symbol,
            listing=filing.listing,
            seq=filing.seq,
            listing_period_end=filing.period_end.isoformat(),
            period_end=values.pop("period_end", ""),
            basis=values.pop("basis", filing.basis),
            format=values.pop("format", ""),
            url=filing.xbrl_url or "",
            **values,
        )

    # --- NSE ----------------------------------------------------------------------------

    def nse(self, filings: Sequence[Filing]) -> list[Outcome]:
        outcomes: list[Outcome] = []
        candidates: list[Candidate] = []
        for filing in filings:
            got = self._candidate(filing)
            if isinstance(got, Outcome):
                outcomes.append(got)
            else:
                candidates.append(got)
        candidates.sort(key=lambda c: (c.period_end, c.basis, _filed_order(c), c.filing.seq))
        for candidate in candidates:
            outcomes.append(self._store(candidate))
        return outcomes

    def _candidate(self, filing: Filing) -> Candidate | Outcome:
        assert filing.xbrl_url is not None
        entry = self.cache.entry(filing.xbrl_url)
        if entry is None:
            return self._outcome(filing, result="excluded", reason="not_fetched")
        if entry.status != 200:
            return self._outcome(filing, result="excluded", reason=f"xbrl_http_{entry.status}")
        if entry.sha256 in self.shas:
            return self._outcome(filing, result="already_stored")
        parsed: Parsed | None = None
        if filing.listing != LISTING_INSURANCE:
            try:
                parsed = parse_xbrl(self.cache.body(entry))
            except XbrlError as exc:
                return self._outcome(filing, result="excluded", reason=str(exc))
        basis = (parsed.basis if parsed else "") or filing.basis
        if not basis:
            return self._outcome(filing, result="excluded", reason="basis_unknown")
        if parsed and year_before_listing(parsed.period_end, filing.period_end):
            logger.info(
                "period a year before the listing's %s %s: listing %s, document %s",
                filing.symbol,
                filing.xbrl_url,
                filing.period_end,
                parsed.period_end,
            )
            return self._outcome(filing, result="excluded", reason="period_year_before_listing")
        period_end, mismatch = document_period(
            parsed.period_end if parsed else None, filing.period_end
        )
        if mismatch:
            logger.info(
                "period mismatch %s %s: listing %s, document %s",
                filing.symbol,
                filing.xbrl_url,
                filing.period_end,
                period_end,
            )
        self.shas.add(entry.sha256)
        return Candidate(
            filing,
            parsed,
            basis,
            period_end,
            entry.sha256,
            str(self.cache.blob_path(entry.sha256)),
            xbrl_at(filing),
        )

    def _store(self, c: Candidate) -> Outcome:
        insurer = c.parsed is None or c.parsed.kind.format == FORMAT_INSURANCE
        fmt = FORMAT_INSURANCE if insurer else c.parsed.kind.format  # type: ignore[union-attr]
        base = {"period_end": c.period_end.isoformat(), "basis": c.basis, "format": fmt}
        quarter, q4 = (None, False) if insurer else self._quarter(c)
        if q4 and quarter is None:
            return self._outcome(
                c.filing, result="excluded", reason="q4_nine_month_missing", **base
            )
        key = (c.period_end, c.basis, SOURCE_NSE)
        held = self.latest.get(key)
        if held is not None and not insurer and _same(held, quarter, _ytd_of(c.parsed, q4)):
            return self._outcome(c.filing, result="same_figures", **base)
        version = 1 if held is None else held.version + 1
        announced = self._announced(c) if version == 1 else None
        when = public_at(version, announced, c.filed, floor=unbroadcast(c.filing))
        if when is None:
            return self._outcome(c.filing, result="excluded", reason="no_time", **base)
        mismatch = document_period(c.period_end, c.filing.period_end)[1]
        row = self._row(c, fmt, quarter, q4, mismatch, when, version, held)
        self.session.add(row)
        self.session.flush()
        self.rows.append(row)
        self._hold(row)
        return self._outcome(
            c.filing,
            result=FORMAT_INSURANCE if insurer else "stored",
            version=version,
            q4_derived=q4,
            period_mismatch=mismatch,
            public_at_source=when.source,
            flags=row.quality_flags or "",
            **base,
        )

    def _quarter(self, c: Candidate) -> tuple[Figures | None, bool]:
        """The quarter's figures and whether they are derived (Q4 = year less nine months);
        (None, True) when Q4 must be derived and the nine months are not known."""
        parsed = c.parsed
        assert parsed is not None
        if parsed.quarter is not None or parsed.annual is None:
            return parsed.quarter, False
        nine = parsed.nine_month or self._nine_months(c.period_end, c.basis, parsed.fy_start)
        return (None if nine is None else derive_q4(parsed.annual, nine)), True

    def _announced(self, c: Candidate) -> datetime | None:
        return self._announced_for(c.filing, c.period_end)

    def _announced_for(self, filing: Filing, period_end: date) -> datetime | None:
        """When the results announcement of `filing` (for `period_end`) was disseminated: the one
        near the filing's day, else the quarter's own results announcement (an XBRL filed days
        after the results PDF; `late_announcement`)."""
        anchor = anchor_date(filing)
        match = match_announcement(self.items, anchor, period_end) if anchor else None
        if match is None:
            found = quarter_announcement(self.items, period_end, self._taken(period_end))
            match = late_announcement(filing, found)
        return match.public if match else None

    def _taken(self, period_end: date) -> set[datetime]:
        """The announcements (`Announcement.public`) that date another quarter's NSE filing, any
        version of it (a revision timed by its XBRL leaves its first version's announcement
        taken)."""
        times = {
            row.public_at
            for row in [*self.rows, *self.older_rows]
            if row.source == SOURCE_NSE
            and row.public_at_source == "announcement"
            and row.period_end != period_end
        }
        return {a.public for a in self.items if a.at in times or a.public in times}

    def _secondary_when(self, period_end: date) -> PublicAt:
        anchor = comparative_quarter(period_end, self.listed_on)
        return _secondary_public_at(self.items, period_end, self._taken(anchor), self.listed_on)

    # --- timing again -------------------------------------------------------------------

    def retime(self, filings: Sequence[Filing]) -> list[Retimed]:
        """Every stored row's `public_at` worked out again (`timing`, the cache as it is now):
        an NSE row from its listing filing (by document URL) and, for a first version, its
        results announcement; a secondary row from its quarter's announcement (its first version;
        a later one was observed in the page). A row that comes out later is moved later; one
        that comes out earlier is moved only onto an announcement's dissemination (a stored time
        that was late); a row no longer in the listing moves only from its announcement's
        submission to that announcement's dissemination, and otherwise keeps its time."""
        by_url = {f.xbrl_url: f for f in filings if f.xbrl_url}
        moved = []
        for row in [*self.rows, *self.older_rows]:
            when, filed = self._when_again(row, by_url)
            if when is None or when.at == row.public_at:
                continue
            if not may_move(row.public_at, when, row.period_end, row.version):
                continue
            moved.append(self._move(row, when, filed))
        if moved:
            self.session.flush()
        return moved

    def _when_again(
        self, row: HatFinancialResult, by_url: dict[str, Filing]
    ) -> tuple[PublicAt | None, FiledAt | None]:
        if row.source == SOURCE_SECONDARY:
            if row.version > 1:
                return None, None
            return self._secondary_when(row.period_end), None
        filing = by_url.get(row.source_url)
        if filing is None:  # gone from the listing: an announcement's time can still be corrected
            return self._disseminated(row), None
        filed = xbrl_at(filing)
        announced = self._announced_for(filing, row.period_end) if row.version == 1 else None
        return public_at(row.version, announced, filed, floor=unbroadcast(filing)), filed

    def _disseminated(self, row: HatFinancialResult) -> PublicAt | None:
        """A first version timed by an announcement's submission (`an_dt`), at that announcement's
        dissemination; None for anything else."""
        if row.version > 1 or row.public_at_source != "announcement":
            return None
        match = next((a for a in self.items if a.at == row.public_at), None)
        return None if match is None else PublicAt(match.public, "announcement", False)

    def _move(self, row: HatFinancialResult, when: PublicAt, filed: FiledAt | None) -> Retimed:
        lead = int((when.at - row.public_at).total_seconds())
        old = (row.public_at, row.public_at_source, row.public_at_estimated)
        old_xbrl = (row.xbrl_at, row.xbrl_at_estimated)
        row.public_at = when.at
        row.public_at_source = when.source
        row.public_at_estimated = when.estimated
        if filed is not None and row.xbrl_at is not None and filed.at > row.xbrl_at:
            row.xbrl_at = filed.at
            row.xbrl_at_estimated = filed.estimated
        return Retimed(
            str(row.id),
            row.symbol,
            row.period_end.isoformat(),
            row.basis,
            row.source,
            row.version,
            old[0].isoformat(),
            old[1],
            when.at.isoformat(),
            when.source,
            lead,
            old[2],
            when.estimated,
            old_xbrl[0].isoformat() if old_xbrl[0] else "",
            old_xbrl[1],
            row.xbrl_at.isoformat() if row.xbrl_at else "",
            row.xbrl_at_estimated,
        )

    def _nine_months(self, period_end: date, basis: str, fy_start: date | None) -> Figures | None:
        q3_end = quarter_ends_before(period_end, 1)[0]
        held = self.latest.get((q3_end, basis, SOURCE_NSE))
        if held is None or held.ytd is None or held.ytd.revenue is None:
            return None
        if fy_start is not None and held.ytd_start not in (None, fy_start):
            return None
        return held.ytd

    def _row(
        self,
        c: Candidate,
        fmt: str,
        quarter: Figures | None,
        q4: bool,
        mismatch: bool,
        when: PublicAt,
        version: int,
        held: Held | None,
    ) -> HatFinancialResult:
        parsed = c.parsed
        ytd = _ytd_of(parsed, q4)
        return HatFinancialResult(
            instrument_id=uuid.UUID(self.member.instrument_id),
            symbol=c.filing.symbol or self.member.symbols[0],
            isin=self.member.isin,
            period_start=(parsed.period_start if parsed else None) or quarter_start(c.period_end),
            period_end=c.period_end,
            basis=c.basis,
            format=fmt,
            source=SOURCE_NSE,
            **_quarter_columns(quarter),
            **_ytd_columns(parsed.ytd_start if parsed else None, ytd),
            q4_derived=q4,
            listing_period_end=c.filing.period_end if mismatch else None,
            period_mismatch=mismatch,
            quality_flags=";".join(parsed.flags) if parsed and parsed.flags else None,
            public_at=when.at,
            public_at_source=when.source,
            public_at_estimated=when.estimated,
            **_xbrl_columns(c.filed),
            version=version,
            supersedes_id=held.id if held else None,
            listing=c.filing.listing,
            listing_seq=c.filing.seq,
            source_url=c.filing.xbrl_url or "",
            sha256=c.sha256,
            raw_path=c.raw_path,
        )

    # --- the secondary source -----------------------------------------------------------

    def secondary(self, quarters: Sequence[PageQuarter], start: date, end: date) -> list[Outcome]:
        outcomes = []
        seen: set[tuple[date, str]] = set()
        for quarter, url, sha, fetched_at in quarters:
            key = (quarter.period_end, quarter.basis)
            if not start <= quarter.period_end <= end or key in seen:
                continue
            seen.add(key)
            if (quarter.period_end, quarter.basis, SOURCE_NSE) in self.latest:
                continue  # NSE gives it: the secondary source only cross-checks it
            figures = Figures(quarter.revenue, None, quarter.net_profit, quarter.eps_basic, None)
            held = self.latest.get((quarter.period_end, quarter.basis, SOURCE_SECONDARY))
            if held is not None and held.figures == figures_key(_normal(figures)):
                continue
            version = 1 if held is None else held.version + 1
            when = (
                self._secondary_when(quarter.period_end)
                if version == 1
                else PublicAt(fetched_at, "observed", True)  # first seen in this page
            )
            row = self._secondary_row(quarter, url, sha, when, version, held)
            self.session.add(row)
            self.session.flush()
            self.rows.append(row)
            self._hold(row)
            outcomes.append(
                Outcome(
                    self.member.instrument_id,
                    self.member.symbols[0],
                    SOURCE_SECONDARY,
                    "",
                    "",
                    quarter.period_end.isoformat(),
                    quarter.basis,
                    FORMAT_SECONDARY,
                    url,
                    "stored",
                    version=version,
                    public_at_source=when.source,
                )
            )
        return outcomes

    def _secondary_row(
        self,
        quarter: Quarter,
        url: str,
        sha: str,
        when: PublicAt,
        version: int,
        held: Held | None,
    ) -> HatFinancialResult:
        return HatFinancialResult(
            instrument_id=uuid.UUID(self.member.instrument_id),
            symbol=self.member.symbols[0],
            isin=self.member.isin,
            period_start=quarter_start(quarter.period_end),
            period_end=quarter.period_end,
            basis=quarter.basis,
            format=FORMAT_SECONDARY,
            source=SOURCE_SECONDARY,
            revenue=quarter.revenue,
            net_profit=quarter.net_profit,
            eps_basic=quarter.eps_basic,
            q4_derived=False,
            period_mismatch=False,
            public_at=when.at,
            public_at_source=when.source,
            public_at_estimated=when.estimated,
            version=version,
            supersedes_id=held.id if held else None,
            listing=SOURCE_SECONDARY,
            listing_seq="",
            source_url=url,
            sha256=sha,
            raw_path=str(self.cache.blob_path(sha)),
        )


def _ytd_of(parsed: Parsed | None, q4: bool) -> Figures | None:
    """The year-to-date figures a row stores: the year for a derived Q4, else the document's."""
    if parsed is None:
        return None
    return parsed.annual if q4 else parsed.ytd


def _ytd_key(ytd: Figures | None) -> tuple[Any, ...]:
    if ytd is None:
        return (None,) * 3
    return (_dec(ytd.revenue), _dec(ytd.net_profit), _dec(ytd.eps_basic))


def _same(held: Held, quarter: Figures | None, ytd: Figures | None) -> bool:
    """Whether a new document repeats the held version: the same quarter and the same year to
    date (a revision of the year to date alone is a new version: Q4 derivation reads it)."""
    return figures_key(_normal(quarter)) == held.figures and _ytd_key(ytd) == _ytd_key(held.ytd)


def _quarter_columns(quarter: Figures | None) -> dict[str, Decimal | None]:
    names = ("revenue", "total_income", "net_profit", "eps_basic", "eps_diluted")
    return dict(zip(names, figures_key(quarter), strict=True))


def _ytd_columns(start: date | None, ytd: Figures | None) -> dict[str, Any]:
    if ytd is None:
        return {"ytd_period_start": start}
    return {
        "ytd_period_start": start,
        "ytd_revenue": ytd.revenue,
        "ytd_net_profit": ytd.net_profit,
        "ytd_eps": ytd.eps_basic,
    }


def _xbrl_columns(filed: FiledAt | None) -> dict[str, Any]:
    if filed is None:
        return {"xbrl_at": None, "xbrl_at_estimated": False}
    return {"xbrl_at": filed.at, "xbrl_at_estimated": filed.estimated}


LATE_FILING = timedelta(days=7)
EARLIEST_MOVE = timedelta(days=120)


def may_move(old_at: datetime, when: PublicAt, period_end: date, version: int = 1) -> bool:
    """Whether a stored time may become `when` (the table's trigger allows no other): later, or,
    for a first version, earlier onto an announcement after the quarter's end and by at most
    `EARLIEST_MOVE`."""
    if when.at >= old_at:
        return True
    return (
        version == 1
        and when.source == "announcement"
        and when.at.astimezone(IST).date() > period_end
        and old_at - when.at <= EARLIEST_MOVE
    )


def late_announcement(filing: Filing, found: Announcement | None) -> Announcement | None:
    """`found` (the quarter's own results announcement) as the time of a first filing whose XBRL
    came after it, or None: never for a revision (a later filing of the quarter carries figures
    that were not public then), nor for a filing made more than `LATE_FILING` after it (the
    figures need not be the announced ones) or not dated at all."""
    if found is None or filing.revision:
        return None
    filed = xbrl_at(filing)
    if filed is None or filed.at - found.public > LATE_FILING:
        return None
    return found


def _secondary_public_at(
    items: Sequence[Announcement],
    period_end: date,
    used: set[datetime],
    listed_on: date | None = None,
) -> PublicAt:
    """A secondary-source quarter's results announcement time (its dissemination), else the
    first results announcement after its deadline (a late filer, or a company listed after the
    deadline: the next results carry the quarter as their preceding one), else its deadline
    (estimated); for a quarter that ended before the listing, those of the quarter whose results
    first carry it (`comparative_quarter`), never before the listing day's end."""
    anchor = comparative_quarter(period_end, listed_on)
    match = quarter_announcement(items, anchor, used)
    if match is not None:
        return PublicAt(match.public, "announcement", False)
    due = deadline(anchor)
    if listed_on is not None:
        due = max(due, end_of(listed_on))
    later = min((a.public for a in items if a.at > due), default=None)
    if later is not None:
        return PublicAt(later, "announcement", False)
    return PublicAt(due, "deadline", True)


def _normal(figures: Figures | None) -> Figures | None:
    if figures is None:
        return None
    return Figures(*(_dec(v) for v in figures_key(figures)))


def _filed_order(c: Candidate) -> tuple[bool, str]:
    return (c.filed is None, c.filed.at.isoformat() if c.filed else "")
