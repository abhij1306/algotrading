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
and so does each announcement of results inside an ISIN's stored history that no stored filing
accounts for (`results_quarter_missing`, dated). Only announcements of results count
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


def missing_quarters(
    stored: Mapping[str, Sequence[date]],
    by_isin: Mapping[str, Sequence[Announcement]],
    reference_end: datetime,
) -> list[tuple[str, datetime]]:
    """Announcements of results (ISIN, time) inside an ISIN's stored history (from its first
    stored period's end to `MATCH_DAYS` after its last, by `reference_end`) that no stored
    filing of the ISIN accounts for (none whose period ended before the announcement and
    within `MATCH_DAYS` of it): a quarter announced but missing from the store."""
    out = []
    for isin, periods in stored.items():
        ends = sorted(set(periods))
        if not ends:
            continue
        last = datetime.combine(ends[-1] + timedelta(days=MATCH_DAYS), datetime.max.time(), IST)
        for item in by_isin.get(isin, ()):
            day = item.announced.date()
            if not item.results or day <= ends[0] or item.announced > min(last, reference_end):
                continue
            # the stored period nearest before the announcement
            i = bisect_left(ends, day) - 1
            if i < 0 or (day - ends[i]).days > MATCH_DAYS:
                out.append((isin, item.announced))
    return out


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
    reference_end = max(a.announced for items in by_isin.values() for a in items)
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
    stored_periods: dict[str, list[date]] = defaultdict(list)
    for f in filings:
        if f.isin:
            stored_periods[f.isin].append(f.period_end)
    holes = missing_quarters(stored_periods, by_isin, reference_end)
    holes = [(i, a) for i, a in holes if ctx.start <= a.date() <= ctx.end]
    summary["quarters_missing"] = len(holes)
    if holes:  # results announced inside the stored history with no stored filing
        found = {
            **finding(
                "results_quarter_missing",
                FAIL,
                [{"isin": i, "announced": a.isoformat()} for i, a in sorted(holes)[:25]],
            ),
            "count": len(holes),
        }
        findings.append(dated(found, [a.date() for _, a in holes]))
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
