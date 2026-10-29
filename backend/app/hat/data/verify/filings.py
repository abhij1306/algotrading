"""D5: when each stored results filing became public, against NSE's announcement record.

The reference is NSE's corporate-announcement feed pulled independently of the results store
(`--ca-data`: `results_announcements_*.csv`, each announcement's time and the exchange's
dissemination time, IST). A stored filing (`hat_financial_result`) is matched to the
announcements of its ISIN (else its symbol) made after its period ended and within
`MATCH_DAYS`, else within a day of its `public_at` (`near`), the one nearest its `public_at`. Then:

- `public_before_dissemination` (fail): `public_at` is more than `LEAD_TOLERANCE` before NSE
  disseminated the matched announcement: the figures are usable before the market saw them.
- `estimate_before_first_announcement` (fail): an estimated `public_at` (the filing deadline, or
  the XBRL day's end) before the first announcement of the period: usable before it was public.
- `estimate_unproven` (fail): an estimated `public_at` where no matched announcement names
  results (only another board-meeting outcome): nothing shows the figures were public.
- `before_any_announcement` (fail): `public_at` before the period's first announcement was
  disseminated, with no announcement within a day of it: the figures are usable before any
  results were public, and nothing in the reference shows the filing (an XBRL filed on its own
  would need its own evidence).
- `between_announcements` (warn): no announcement within a day of `public_at`, but the period's
  first announcement was disseminated before it (cited): the results were public by then.
- `late_vs_announcement` (warn): `public_at` more than a day after the matched announcement
  (safe, but the figures reach the engine late).
- `unmatched` (fail): no announcement of the instrument in the window: its timing is unproven.

No reference files fails the check; a filing public after the reference's last announcement is
outside its coverage (the check's `coverage` ends there), and every failed finding is dated by
the filings' `public_at`.

Results announced in years the store holds no filing of (the pre-2020 gap) fail for those years,
and so does each period a company must have stored and has not (`results_quarter_missing`,
`missing_quarters`, a calendar rule: `demanded_periods`). The companies are the results store's
scope (NIFTY 500 point-in-time members from `FIRST_PERIOD_END`, stored rows or not) and every ISIN
with stored rows, each judged as one company over its linked ISIN changes (`lineage_chains`): a
quarter filed under any ISIN of the chain counts. A company owes every quarter end from the
store's first quarter or the quarter it listed in, when later, through the last one whose filing
deadline is on or before both the run's `as_of` (the earliest of `--to`, the bars' last session and
today) and its last traded day. Nothing stored sets the start. Announcements only date such a
finding (the first announcement of results after the period, by its deadline plus `GRACE`, else
the deadline), never excuse it. Only announcements of results count
(`announcement_kind`): clarifications, replies and delay notices are left out, and a board
meeting's outcome that does not name results is matched but never proves they were public.
An XBRL-timed filing at or after the exchange's broadcast of that XBRL in NSE's results listing
(the raw cache it was parsed from) is in order. A failure on the reviewed exceptions list
(`D5_EXCEPTIONS`: code, ISIN, period end or `*`, and the reason) is reported as `excepted` (warn),
and an exception that no failure needs as `exception_unused` (warn).
The summary counts the filings per year and source, the 23:59 `public_at` times by how they were
made (`public_at_source`), the reference's announcements in the years the store has none of (the
pre-2020 gap), and the fundamentals figures that are not point in time (screener.in pages).
"""

from __future__ import annotations

import csv
import re
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.paths import D5_EXCEPTIONS

from ...time import IST
from ..audit import FAIL, WARN, check_result, finding
from ..results.__main__ import FIRST_PERIOD_END, NSE_RAW
from ..results.nse import (
    INTEGRATED_API,
    LISTING_INTEGRATED,
    LISTING_REG33,
    RESULTS_API,
    filings_of,
)
from ..results.raw import RawCache
from . import covered, dated, spanning
from .context import Context

MATCH_DAYS = 120  # a quarter's results are announced within 45 (60 for March) days, or late
LEAD_TOLERANCE = timedelta(seconds=60)
LATE = timedelta(days=1)
ESTIMATED_SOURCES = frozenset({"deadline", "xbrl_date"})
_REF_GLOB = "results_announcements_*.csv"
_TIME_FORMAT = "%d-%b-%Y %H:%M:%S"


@dataclass(frozen=True)
class Announcement:
    announced: datetime
    disseminated: datetime | None
    results: bool = True  # names results; False: a board meeting's outcome that does not


_NOT_RESULTS = (
    "clarification",
    "reply to clarification",
    "reasons for delayed",
    "option to submit",
)
_NAMES_RESULTS = re.compile(r"financial result|financials|withFR|_FR[._]|audited", re.IGNORECASE)


def announcement_kind(desc: str, text: str, file: str) -> str | None:
    """`results` (an announcement of results, or a board meeting's outcome that names them),
    `meeting` (another outcome of a board meeting: matched, but no proof results were public)
    or None (a clarification, a reply, a delay notice: not an announcement of results)."""
    kind = desc.strip().lower()
    if kind.startswith(_NOT_RESULTS):
        return None
    if kind.startswith("outcome of board meeting"):
        return (
            "results" if _NAMES_RESULTS.search(text) or _NAMES_RESULTS.search(file) else "meeting"
        )
    return "results" if "financial result" in kind or "audited" in kind else None


@dataclass(frozen=True)
class Filing:
    symbol: str
    isin: str
    period_end: date
    source: str
    public_at: datetime
    public_at_source: str
    source_url: str = ""


def parse_time(raw: str) -> datetime | None:
    """NSE's `30-Oct-2021 23:06:07` (IST) as an aware instant; '-' or blank is None."""
    text = (raw or "").strip()
    try:
        return datetime.strptime(text, _TIME_FORMAT).replace(tzinfo=IST)
    except ValueError:
        return None


def read_announcements(
    paths: Iterable[Path],
) -> tuple[dict[str, list[Announcement]], dict[str, list[Announcement]], Counter[int]]:
    """The announcements of results (`announcement_kind`: clarifications, replies and delay
    notices left out) by ISIN and by symbol, each sorted by time, and their count per year."""
    by_isin: dict[str, list[Announcement]] = defaultdict(list)
    by_symbol: dict[str, list[Announcement]] = defaultdict(list)
    years: Counter[int] = Counter()
    seen: set[tuple[str, str, datetime]] = set()
    for path in paths:
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                announced = parse_time(row.get("announcement_dt", ""))
                kind = announcement_kind(
                    row.get("desc", ""), row.get("text", ""), row.get("file", "")
                )
                if announced is None or kind is None:
                    continue
                isin, symbol = row.get("isin", "").strip(), row.get("symbol", "").strip()
                key = (isin, symbol, announced)
                if key in seen:  # the windows' files overlap at their edges
                    continue
                seen.add(key)
                item = Announcement(
                    announced,
                    parse_time(row.get("exchange_dissemination_dt", "")),
                    results=kind == "results",
                )
                years[announced.year] += 1
                if isin:
                    by_isin[isin].append(item)
                if symbol:
                    by_symbol[symbol].append(item)
    for table in (by_isin, by_symbol):
        for items in table.values():
            items.sort(key=lambda a: a.announced)
    return by_isin, by_symbol, years


def last_announcement(*tables: Mapping[str, Sequence[Announcement]]) -> datetime | None:
    """The latest announcement in any of the tables (by ISIN, by symbol: a row may carry only a
    symbol); None when they hold none."""
    return max((a.announced for t in tables for items in t.values() for a in items), default=None)


def candidates(items: Sequence[Announcement], period_end: date) -> list[Announcement]:
    """The announcements (sorted) made after `period_end` and within `MATCH_DAYS` of it."""
    first = datetime.combine(period_end + timedelta(days=1), datetime.min.time(), IST)
    last = first + timedelta(days=MATCH_DAYS)
    keys = [a.announced for a in items]
    return list(items[bisect_left(keys, first) : bisect_right(keys, last)])


def near(items: Sequence[Announcement], period_end: date, at: datetime) -> list[Announcement]:
    """The announcements (sorted) after `period_end` public (disseminated, else announced) within
    `LATE` of `at`: a quarter that ended before the listing counts from a later quarter's results
    (`timing.comparative_quarter`), outside `candidates`' window."""
    return [
        a
        for a in items
        if a.announced.date() > period_end and abs((a.disseminated or a.announced) - at) <= LATE
    ]


def judge(
    filing: Filing, found: Sequence[Announcement], broadcast: datetime | None = None
) -> tuple[str | None, dict[str, Any]]:
    """A filing's finding code (None: in order) and what it was judged on. A `public_at` from
    the XBRL (`xbrl`) at or after the exchange's broadcast of that XBRL in NSE's results listing
    (`broadcast`, the source file) is in order: the exchange published the figures then, whatever
    the announcements show."""
    if (
        filing.public_at_source == "xbrl"
        and broadcast is not None
        and filing.public_at >= broadcast - LEAD_TOLERANCE
    ):
        return None, {"xbrl_broadcast": broadcast.isoformat()}
    if not found:
        return "unmatched", {}
    # the announcement whose public time (dissemination, else announcement) is nearest: a
    # filing timed by one announcement is judged by that one, not a neighbour made earlier
    nearest = min(found, key=lambda a: abs((a.disseminated or a.announced) - filing.public_at))
    # only an announcement that names results proves they were public (a board meeting's other
    # outcome does not)
    named = next((a for a in found if a.results), None)
    first = None if named is None else named.disseminated or named.announced
    detail = {
        "announced": nearest.announced.isoformat(),
        "disseminated": None if nearest.disseminated is None else nearest.disseminated.isoformat(),
        "first_public": None if first is None else first.isoformat(),
    }
    if filing.public_at_source in ESTIMATED_SOURCES:
        if named is None:  # an estimate no announcement of results shows public
            return "estimate_unproven", detail
        if filing.public_at < named.announced:
            return "estimate_before_first_announcement", detail
    seen = nearest.disseminated or nearest.announced
    if filing.public_at < nearest.announced - LATE:  # no announcement near it
        if first is None or filing.public_at < first - LEAD_TOLERANCE:
            return "before_any_announcement", detail
        return "between_announcements", detail
    # early against the nearest announcement, but after one that named the results: public then
    if filing.public_at < seen - LEAD_TOLERANCE and (
        first is None or filing.public_at < first - LEAD_TOLERANCE
    ):
        lead = seen - filing.public_at
        return "public_before_dissemination", {**detail, "lead_s": int(lead.total_seconds())}
    if filing.public_at - nearest.announced > LATE:
        return "late_vs_announcement", detail
    return None, detail


DEADLINE_DAYS = 45  # SEBI: a quarter's results within 45 days of its end, 60 for March


def deadline(quarter: date) -> date:
    """The last day a quarter's results are due."""
    return quarter + timedelta(days=60 if quarter.month == 3 else DEADLINE_DAYS)  # noqa: PLR2004


GRACE = timedelta(days=30)  # an announcement this long after a deadline still dates its period


def quarter_of(day: date) -> date:
    """The end of the calendar quarter `day` is in."""
    month = 3 * ((day.month - 1) // 3) + 3
    return date(day.year, month, 31 if month in {3, 12} else 30)


def next_quarter(quarter: date) -> date:
    """The quarter end after `quarter` (a quarter end)."""
    return quarter_of(quarter + timedelta(days=1))


@dataclass(frozen=True)
class Listing:
    """A company's trading span: its listing (or first traded) day and last traded day."""

    first: date | None
    last: date | None


def lineage_chains(links: Iterable[tuple[str, str]], isins: Iterable[str]) -> list[tuple[str, ...]]:
    """Each company's ISINs (sorted): `isins` joined through the linked ISIN changes `links`
    (old, new; `hat_isin_lineage`), so a quarter filed under either ISIN counts for both."""
    parent: dict[str, str] = {}

    def root(isin: str) -> str:
        while parent.setdefault(isin, isin) != isin:
            parent[isin] = parent[parent[isin]]
            isin = parent[isin]
        return isin

    for old, new in links:
        parent[root(old)] = root(new)
    groups: dict[str, set[str]] = defaultdict(set)
    for isin in isins:
        groups[root(isin)].add(isin)
    for isin in list(parent):  # the rest of each chain the given ISINs reach
        if root(isin) in groups:
            groups[root(isin)].add(isin)
    return sorted(tuple(sorted(g)) for g in groups.values())


def chain_listing(chain: Iterable[str], listings: Mapping[str, Listing]) -> Listing:
    """A chain's span: its first ISIN's listing to its last ISIN's last traded day."""
    spans = [listings[i] for i in chain if i in listings]
    firsts = [s.first for s in spans if s.first is not None]
    lasts = [s.last for s in spans if s.last is not None]
    return Listing(min(firsts, default=None), max(lasts, default=None))


def demanded_periods(listing: Listing, coverage_start: date, as_of: date) -> list[date]:
    """The quarter ends a company must have stored: from the store's first quarter
    (`coverage_start`, `FIRST_PERIOD_END`) or the quarter it listed in when later, through the
    last one whose filing deadline is on or before both `as_of` and its last traded day (a
    company delisted, merged or suspended before a deadline need not file for that quarter).
    Nothing stored and no announcement plays a part: a missing one proves nothing. Every
    company is held to quarters: the store keeps no period length (`period_start` is always a
    quarter), and NIFTY 500 companies file quarterly (SEBI LODR 33)."""
    first = quarter_of(coverage_start)
    if listing.first is not None and quarter_of(listing.first) > first:
        first = quarter_of(listing.first)
    last = as_of if listing.last is None else min(as_of, listing.last)
    out = []
    at = first
    while deadline(at) <= last:
        out.append(at)
        at = next_quarter(at)
    return out


def missing_quarters(  # noqa: PLR0913
    chains: Iterable[Sequence[str]],
    stored: Mapping[str, Iterable[date]],
    listings: Mapping[str, Listing],
    by_isin: Mapping[str, Sequence[Announcement]],
    coverage_start: date,
    as_of: date,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """Each demanded period (`demanded_periods`, over the chain's span) no ISIN of a company's
    chain has stored, dated (`day`) by the chain's first announcement of results after the period's
    end and by its deadline plus `GRACE` (one before `start` too), else by its deadline; kept when
    that day is in `start..end`. The item names the chain's latest ISIN (`isin`) and all of its
    ISINs (`isins`). Announcements only date a finding, never excuse one."""
    out = []
    for chain in chains:
        have = {p for isin in chain for p in stored.get(isin, ())}
        said = sorted(
            a.announced.date() for isin in chain for a in by_isin.get(isin, ()) if a.results
        )
        span = chain_listing(chain, listings)
        latest = max(
            chain, key=lambda i: (listings.get(i, Listing(None, None)).first or date.min, i)
        )
        for period in demanded_periods(span, coverage_start, as_of):
            if period in have:
                continue
            due = deadline(period)
            day = next((d for d in said if period < d <= due + GRACE), due)
            if start <= day <= end:
                out.append(
                    {
                        "isin": latest,
                        "isins": list(chain),
                        "quarter": period.isoformat(),
                        "day": day.isoformat(),
                    }
                )
    return sorted(out, key=lambda i: (i["isin"], i["quarter"]))


def lead_bucket(seconds: int) -> str:
    return "1-5 min" if seconds <= 300 else "5-60 min" if seconds <= 3600 else "over 1 h"


def by_chain(
    by_isin: Mapping[str, Sequence[Announcement]], chains: Iterable[Sequence[str]]
) -> dict[str, list[Announcement]]:
    """The announcements by ISIN, each ISIN of a chain holding the whole chain's (sorted): a
    quarter filed under a company's new ISIN is announced under its old one, or the reverse."""
    out = {isin: list(items) for isin, items in by_isin.items()}
    for chain in chains:
        if len(chain) < 2:  # noqa: PLR2004 - a chain of one ISIN has nothing to merge
            continue
        merged = sorted((a for i in chain for a in by_isin.get(i, ())), key=lambda a: a.announced)
        for isin in chain:
            out[isin] = merged
    return out


def judge_all(
    filings: Iterable[Filing],
    by_isin: Mapping[str, Sequence[Announcement]],
    by_symbol: Mapping[str, Sequence[Announcement]],
    reference_end: datetime,
    broadcasts: Mapping[str, datetime] | None = None,
) -> tuple[Counter[str], dict[str, list[dict[str, Any]]], dict[str, Counter[str]]]:
    """Every filing public by `reference_end` (the reference's last announcement) judged: the
    counts per code, the items per code, and the leads before dissemination by year and size.
    Later filings are counted as `after_reference`. `broadcasts`: the XBRL listing's broadcast
    time by document URL (`judge`)."""
    counts: Counter[str] = Counter()
    items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    leads: dict[str, Counter[str]] = defaultdict(Counter)
    for filing in filings:
        if filing.public_at > reference_end:
            counts["after_reference"] += 1
            continue
        found = (
            candidates(by_isin.get(filing.isin, ()), filing.period_end)
            or candidates(by_symbol.get(filing.symbol, ()), filing.period_end)
            or near(by_isin.get(filing.isin, ()), filing.period_end, filing.public_at)
            or near(by_symbol.get(filing.symbol, ()), filing.period_end, filing.public_at)
        )
        code, detail = judge(filing, found, (broadcasts or {}).get(filing.source_url))
        counts["judged"] += 1
        if code is None:
            counts["in_order"] += 1
            continue
        counts[code] += 1
        if code == "public_before_dissemination":
            leads[str(filing.public_at.year)][lead_bucket(detail["lead_s"])] += 1
        items[code].append(
            {
                "symbol": filing.symbol,
                "isin": filing.isin,
                "period_end": filing.period_end.isoformat(),
                "source": filing.source,
                "public_at": filing.public_at.astimezone(IST).isoformat(),
                "public_at_source": filing.public_at_source,
                **detail,
            }
        )
    for code in items:  # the largest leads, the most telling examples, first
        items[code].sort(key=lambda i: -i.get("lead_s", 0))
    return counts, items, leads


@dataclass(frozen=True)
class Excepted:
    """A reviewed exception (`D5_EXCEPTIONS`): a finding `code` for a company (any ISIN of its
    chain) and a period end (`*`: every period) that is explained and known, with why."""

    code: str
    isin: str
    period_end: str
    reason: str


def read_exceptions(path: Path) -> list[Excepted]:
    """The reviewed exceptions file (CSV: code, isin, symbol for the reader, period_end, reason);
    every row must give a reason. A missing file is no exceptions."""
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        rows = [
            Excepted(r["code"].strip(), r["isin"].strip(), r["period_end"].strip(), r["reason"])
            for r in csv.DictReader(handle)
        ]
    unexplained = [r for r in rows if not r.reason.strip()]
    if unexplained:
        raise ValueError(f"D5 exceptions without a reason: {unexplained[:3]}")
    return rows


def except_items(
    code: str, items: Sequence[Mapping[str, Any]], exceptions: Sequence[Excepted]
) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]], set[Excepted]]:
    """`items` of finding `code` split into those still failing and those a reviewed exception
    covers (each with its `reason`), and the exceptions used. An item names its company by
    `isin` (and `isins`, its chain) and its period by `quarter` or `period_end`."""
    rules = defaultdict(list)
    for rule in exceptions:
        if rule.code == code:
            rules[rule.isin].append(rule)
    failing: list[Mapping[str, Any]] = []
    excepted: list[dict[str, Any]] = []
    used: set[Excepted] = set()
    for item in items:
        period = item.get("quarter") or item.get("period_end")
        match = next(
            (
                rule
                for isin in item.get("isins") or [item.get("isin")]
                for rule in rules.get(isin, ())
                if rule.period_end in {"*", period}
            ),
            None,
        )
        if match is None:
            failing.append(item)
        else:
            used.add(match)
            excepted.append({"code": code, **item, "reason": match.reason})
    return failing, excepted, used


SEVERITY = {
    "results_quarter_missing": FAIL,
    "public_before_dissemination": FAIL,
    "estimate_before_first_announcement": FAIL,
    "estimate_unproven": FAIL,
    "before_any_announcement": FAIL,
    "between_announcements": WARN,
    "late_vs_announcement": WARN,
    "unmatched": FAIL,
}


def run(ctx: Context) -> dict[str, Any]:  # pragma: no cover - real data
    start = datetime.combine(ctx.start, datetime.min.time(), IST)
    end = datetime.combine(ctx.end + timedelta(days=1), datetime.min.time(), IST)
    rows = ctx.rows(
        "SELECT symbol, isin, period_end, source, public_at, public_at_source, source_url "
        "FROM hat_financial_result WHERE public_at >= :start AND public_at < :end",
        start=start.astimezone(UTC),
        end=end.astimezone(UTC),
    )
    filings = [Filing(*r) for r in rows]
    per_year: dict[str, Counter[str]] = defaultdict(Counter)
    late_night: dict[str, int] = Counter()
    for f in filings:
        per_year[str(f.period_end.year)][f.source] += 1
        local = f.public_at.astimezone(IST)
        if local.hour == 23 and local.minute == 59:
            late_night[f.public_at_source] += 1
    pit = ctx.rows(
        "SELECT source, point_in_time, count(*) FROM hat_fundamental_filing GROUP BY 1, 2"
    )
    summary: dict[str, Any] = {
        "filings": len(filings),
        "by_period_year": {k: dict(v) for k, v in sorted(per_year.items())},
        "public_at_2359_by_source": dict(late_night),
        "public_at_2359_explained": (
            "deadline: no announcement found, public_at is the filing deadline (period end + 45 "
            "days, 60 for March) at 23:59 IST; xbrl_date: only the XBRL file's date is known, "
            "public_at is that day's end (23:59:59); the rest are real filing times"
        ),
        "fundamental_filings": [{"source": s, "point_in_time": p, "filings": n} for s, p, n in pit],
    }
    findings = []
    stored_years = {f.period_end.year for f in filings}
    if ctx.ca_data is None or not any(ctx.ca_data.glob(_REF_GLOB)):
        ctx.note("D5: no --ca-data results_announcements files; timing unchecked")
        findings.append(finding("reference_missing", FAIL, [_REF_GLOB]))
        return check_result("D5", summary, findings)
    by_isin, by_symbol, ref_years = read_announcements(sorted(ctx.ca_data.glob(_REF_GLOB)))
    reference_end = last_announcement(by_isin, by_symbol)
    if reference_end is None:
        ctx.note("D5: the results_announcements files hold no announcement of results")
        findings.append(finding("reference_missing", FAIL, [_REF_GLOB]))
        return check_result("D5", summary, findings)
    summary["reference_end"] = reference_end.isoformat()
    # every stored period by ISIN, whenever it became public (a filing public after the window
    # still stores its quarter)
    stored: dict[str, set[date]] = defaultdict(set)
    for isin, period_end in ctx.rows(
        "SELECT DISTINCT isin, period_end FROM hat_financial_result WHERE isin IS NOT NULL"
    ):
        stored[isin].add(period_end)
    listings = _listings(ctx)
    as_of = min(ctx.end, _data_end(ctx), date.today())
    chains = lineage_chains(_links(ctx), set(stored) | _scope(ctx, as_of))
    by_isin = by_chain(by_isin, chains)
    counts, items, leads = judge_all(filings, by_isin, by_symbol, reference_end, _broadcasts(ctx))
    summary["judged"] = dict(counts)
    summary["leads_before_dissemination"] = {k: dict(v) for k, v in sorted(leads.items())}
    holes = missing_quarters(
        chains, stored, listings, by_isin, FIRST_PERIOD_END, as_of, ctx.start, ctx.end
    )
    items["results_quarter_missing"] = holes
    summary["companies_judged"] = len(chains)
    summary["quarters_demanded_as_of"] = as_of.isoformat()
    exceptions = read_exceptions(D5_EXCEPTIONS)
    excepted: list[dict[str, Any]] = []
    used: set[Excepted] = set()
    for code, severity in SEVERITY.items():
        kept, out, hit = except_items(code, items.get(code, []), exceptions)
        excepted += out
        used |= hit
        if not kept:
            continue
        found = {**finding(code, severity, kept), "count": len(kept)}
        if code == "results_quarter_missing":
            found = dated(found, [i["day"] for i in kept])
        elif severity == FAIL:
            found = dated(found, [i["public_at"][:10] for i in kept])
        findings.append(found)
    summary["quarters_missing"] = sum(1 for i in holes if i not in excepted)
    summary["quarters_missing_by_quarter"] = dict(
        sorted(Counter(i["quarter"] for i in holes).items())
    )
    summary["excepted"] = dict(Counter(i["code"] for i in excepted))
    if excepted:
        findings.append(finding("excepted", WARN, excepted, count=len(excepted)))
    unused = [asdict(e) for e in exceptions if e not in used]
    if unused:  # a listed exception nothing needs: the list is out of date
        findings.append(finding("exception_unused", WARN, unused))
    gap = {
        str(y): n
        for y, n in sorted(ref_years.items())
        if ctx.start.year <= y <= ctx.end.year and y < min(stored_years, default=y + 1)
    }
    if gap:  # results announced in years the store holds no filing of: a hole, not a look
        found = finding(
            "no_results_before_store",
            FAIL,
            [{"year": y, "announcements": n} for y, n in gap.items()],
        )
        years = sorted(int(y) for y in gap)
        findings.append(spanning(found, date(years[0], 1, 1), date(years[-1], 12, 31)))
    not_pit = sum(n for _, p, n in pit if not p)
    if not_pit:
        findings.append(
            finding(
                "not_point_in_time_figures",
                WARN,
                [{"source": s, "filings": n} for s, p, n in pit if not p],
            )
        )
    return covered(check_result("D5", summary, findings), ctx.start, reference_end.date())


_TRADED_SQL = """
SELECT CAST(instrument_id AS VARCHAR), min(date), max(date) FROM bars_daily_raw GROUP BY 1
"""


def _listings(ctx: Context) -> dict[str, Listing]:  # pragma: no cover - real data
    """Each ISIN's listing (its listing day, else first traded day) and last traded day."""
    with ctx.store.connect() as con:
        traded = {i: (a, b) for i, a, b in con.execute(_TRADED_SQL).fetchall()}
    out = {}
    for instrument, isin, listed in ctx.rows(
        "SELECT CAST(id AS TEXT), isin, listed_on FROM hat_instrument"
    ):
        first, last = traded.get(instrument, (None, None))
        out[isin] = Listing(listed or first, last)
    return out


def _data_end(ctx: Context) -> date:  # pragma: no cover - real data
    """The last session the daily bars hold."""
    with ctx.store.connect() as con:
        return con.execute("SELECT max(date) FROM bars_daily_raw").fetchone()[0]


def _links(ctx: Context) -> list[tuple[str, str]]:  # pragma: no cover - real data
    """The linked ISIN changes (old ISIN, new ISIN)."""
    return [
        (old, new)
        for old, new in ctx.rows(
            "SELECT o.isin, n.isin FROM hat_isin_lineage l "
            "JOIN hat_instrument o ON o.id = l.old_instrument_id "
            "JOIN hat_instrument n ON n.id = l.new_instrument_id WHERE l.status = 'linked'"
        )
    ]


def _scope(ctx: Context, as_of: date) -> set[str]:  # pragma: no cover - real data
    """The ISINs the results store covers (`results.universe`, NIFTY 500 point-in-time members
    from the store's first quarter), stored rows or not."""
    from ..results.universe import members  # noqa: PLC0415 - the check's own import cost

    return {m.isin for m in members(ctx.session, ctx.store, FIRST_PERIOD_END, as_of)}


def _broadcasts(ctx: Context) -> dict[str, datetime]:  # pragma: no cover - real data
    """Each XBRL document's broadcast time in NSE's results listings (the raw cache the store
    was parsed from), the latest when listed more than once."""
    cache = RawCache(ctx.root / NSE_RAW)
    answers = []
    for url, entry in cache.entries().items():
        if entry.status != 200:  # noqa: PLR2004
            continue
        if url.startswith(INTEGRATED_API):
            answers.append((LISTING_INTEGRATED, cache.body(entry)))
        elif url.startswith(RESULTS_API):
            answers.append((LISTING_REG33, cache.body(entry)))
    out: dict[str, datetime] = {}
    for filing in filings_of(answers):
        if filing.xbrl_url and filing.broadcast_at is not None:
            out[filing.xbrl_url] = max(
                out.get(filing.xbrl_url, filing.broadcast_at), filing.broadcast_at
            )
    return out
