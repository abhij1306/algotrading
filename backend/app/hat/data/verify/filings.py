"""D5: when each stored results filing became public, against NSE's announcement record.

The reference is NSE's corporate-announcement feed pulled independently of the results store
(`--ca-data`: `results_announcements_*.csv`, each announcement's time and the exchange's
dissemination time, IST). A stored filing (`hat_financial_result`) is matched to the
announcements of its ISIN (else its symbol) made after its period ended and within
`MATCH_DAYS`, the one nearest its `public_at`. Then:

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
and so does each period an ISIN must have stored and has not (`results_quarter_missing`,
`missing_quarters`, a calendar rule: `demanded_periods`): every quarter end (half-year ends in a
year whose stored periods are all six months long) from its first stored period, or its listing
quarter when earlier and inside the store's coverage, through the last one past its filing
deadline that ended before its last traded day. Announcements only date such a finding (the
first announcement of results after the period, by its deadline plus `GRACE`, else the
deadline), never excuse it. Only announcements of results count
(`announcement_kind`): clarifications, replies and delay notices are left out, and a board
meeting's outcome that does not name results is matched but never proves they were public.
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
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ...time import IST
from ..audit import FAIL, WARN, check_result, finding
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


def judge(filing: Filing, found: Sequence[Announcement]) -> tuple[str | None, dict[str, Any]]:
    """A filing's finding code (None: in order) and what it was judged on."""
    if not found:
        return "unmatched", {}
    nearest = min(found, key=lambda a: abs(a.announced - filing.public_at))
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
    if filing.public_at < seen - LEAD_TOLERANCE:
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
class Stored:
    """One stored period of an ISIN: its end and its length in months (None: unknown)."""

    end: date
    months: int | None = 3


@dataclass(frozen=True)
class Listing:
    """An ISIN's trading span: its listing (or first traded) day and last traded day."""

    first: date | None
    last: date | None


def _half_years(periods: Iterable[Stored]) -> dict[int, set[int]]:
    """The calendar years an ISIN reported half-yearly, by the period lengths it stored: a year
    whose stored periods are all six months long -> the two half-year end months they fall on."""
    by_year: dict[int, list[Stored]] = defaultdict(list)
    for period in periods:
        by_year[period.end.year].append(period)
    return {
        year: {m for p in items for m in (p.end.month, (p.end.month + 5) % 12 + 1)}
        for year, items in by_year.items()
        if all(p.months == 6 for p in items)  # noqa: PLR2004 - a half year
    }


def demanded_periods(
    periods: Sequence[Stored], listing: Listing, coverage_start: date, as_of: date
) -> list[date]:
    """The period ends an ISIN must have stored: every quarter end from the first period that
    should exist (its first stored period, or the quarter it listed in when earlier and not
    before `coverage_start`) through the last one whose filing deadline has passed by `as_of`
    and that ended before its last traded day. In a calendar year whose stored periods are all
    six months long only the half-year ends are due; a year with nothing stored keeps the
    frequency of the year before. Announcements play no part: a missing one proves nothing."""
    if not periods:
        return []
    first = quarter_of(min(p.end for p in periods))
    if listing.first is not None and coverage_start <= quarter_of(listing.first) < first:
        first = quarter_of(listing.first)
    half = _half_years(periods)
    years = {p.end.year for p in periods}
    out = []
    at, half_months = first, None
    while deadline(at) <= as_of and (listing.last is None or at < listing.last):
        if at.year in years:
            half_months = half.get(at.year)
        if half_months is None or at.month in half_months:
            out.append(at)
        at = next_quarter(at)
    return out


def missing_quarters(  # noqa: PLR0913
    stored: Mapping[str, Sequence[Stored]],
    listings: Mapping[str, Listing],
    by_isin: Mapping[str, Sequence[Announcement]],
    coverage_start: date,
    as_of: date,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """Each demanded period (`demanded_periods`) an ISIN has not stored, dated (`day`) by its
    first announcement of results in `start..end` after the period's end and by its deadline
    plus `GRACE`, else by its deadline; kept when that day is in `start..end`. Announcements
    only date a finding, never excuse one."""
    out = []
    for isin, periods in stored.items():
        have = {p.end for p in periods}
        said = sorted(
            a.announced.date()
            for a in by_isin.get(isin, ())
            if a.results and start <= a.announced.date() <= end
        )
        listing = listings.get(isin, Listing(None, None))
        for period in demanded_periods(periods, listing, coverage_start, as_of):
            if period in have:
                continue
            due = deadline(period)
            day = next((d for d in said if period < d <= due + GRACE), due)
            if start <= day <= end:
                out.append(
                    {
                        "isin": isin,
                        "quarter": period.isoformat(),
                        "day": day.isoformat(),
                        "why": "missing quarter",
                    }
                )
    return sorted(out, key=lambda i: (i["isin"], i["quarter"]))


def lead_bucket(seconds: int) -> str:
    return "1-5 min" if seconds <= 300 else "5-60 min" if seconds <= 3600 else "over 1 h"


def judge_all(
    filings: Iterable[Filing],
    by_isin: Mapping[str, Sequence[Announcement]],
    by_symbol: Mapping[str, Sequence[Announcement]],
    reference_end: datetime,
) -> tuple[Counter[str], dict[str, list[dict[str, Any]]], dict[str, Counter[str]]]:
    """Every filing public by `reference_end` (the reference's last announcement) judged: the
    counts per code, the items per code, and the leads before dissemination by year and size.
    Later filings are counted as `after_reference`."""
    counts: Counter[str] = Counter()
    items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    leads: dict[str, Counter[str]] = defaultdict(Counter)
    for filing in filings:
        if filing.public_at > reference_end:
            counts["after_reference"] += 1
            continue
        found = candidates(by_isin.get(filing.isin, ()), filing.period_end) or candidates(
            by_symbol.get(filing.symbol, ()), filing.period_end
        )
        code, detail = judge(filing, found)
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


SEVERITY = {
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
        "SELECT symbol, isin, period_end, source, public_at, public_at_source "
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
    counts, items, leads = judge_all(filings, by_isin, by_symbol, reference_end)
    summary["judged"] = dict(counts)
    summary["leads_before_dissemination"] = {k: dict(v) for k, v in sorted(leads.items())}
    for code, severity in SEVERITY.items():
        if counts[code]:
            found = {**finding(code, severity, items[code]), "count": counts[code]}
            if severity == FAIL:
                found = dated(found, [i["public_at"][:10] for i in items[code]])
            findings.append(found)
    # every stored period, whenever it became public (a filing public after the window still
    # stores its quarter), with its length
    stored_periods: dict[str, list[Stored]] = defaultdict(list)
    for isin, period_end, months in ctx.rows(
        "SELECT DISTINCT isin, period_end, (extract(year from age(period_end + 1, period_start)) "
        "* 12 + extract(month from age(period_end + 1, period_start)))::int "
        "FROM hat_financial_result WHERE isin IS NOT NULL"
    ):
        stored_periods[isin].append(Stored(period_end, months))
    holes = missing_quarters(
        stored_periods,
        _listings(ctx),
        by_isin,
        min((p.end for ps in stored_periods.values() for p in ps), default=ctx.end),
        ctx.end,
        ctx.start,
        ctx.end,
    )
    summary["quarters_missing"] = len(holes)
    summary["quarters_missing_by_quarter"] = dict(
        sorted(Counter(i["quarter"] for i in holes).items())
    )
    if holes:
        found = {**finding("results_quarter_missing", FAIL, holes[:25]), "count": len(holes)}
        findings.append(dated(found, [i["day"] for i in holes]))
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
