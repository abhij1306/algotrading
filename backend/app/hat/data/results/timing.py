"""When a filing's figures became public (`public_at`) and when its XBRL was filed (`xbrl_at`).
Pure. Why each rule (A0 probe report, sections 5 and 6):

- The results PDF goes out as a corporate announcement ('Financial Result Updates', or a
  board-meeting outcome naming the results in the Integrated Filing era) 31 minutes to 22.5 hours
  before the XBRL, so `public_at` is the earlier of that announcement and `xbrl_at`. An
  announcement counts from the exchange's dissemination (`Announcement.public`), never from the
  company's submission time: NSE disseminated 1-5 minutes later through 2022, and up to days later
  on some days (D5).
- The listing's broadcast time is a later re-broadcast for many Reg 33 filings before 2022. The
  XBRL file name carries the filing date, but its time is a 12-hour clock without AM/PM: only the
  date is ever used. `xbrl_at` is the listing's broadcast time when it falls on that date, else
  the end of that date (`estimated`: an upper bound, never early). Integrated filings carry the
  document's creation time and the broadcast time: the later of the two.
- A revision (version 2 on) is public from its own filing, never from the original's
  announcement.
- One company's announcement times read 12 hours early (05:08 for an evening board meeting). An
  announcement before 09:15 whose XBRL was filed, at a known time on the same day, 12 hours or
  more later is read as the PM time.
- A quarter that ended before the company listed (a secondary-source quarter of a new listing,
  or of one that moved to NSE) was not public on NSE then: it counts from the results that carry
  it as their year-ago comparative, and never before the first results after the listing
  (`comparative_quarter`).
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ...time import IST
from .nse import Announcement, Filing, is_results_announcement

WINDOW_BEFORE = timedelta(days=2)
WINDOW_AFTER = timedelta(days=1)
MARKET_OPEN = time(9, 15)
HALF_DAY = timedelta(hours=12)
END_OF_DAY = time(23, 59, 59)
DEADLINE_DAYS = 45  # Reg 33: results within 45 days of the quarter end
DEADLINE_DAYS_Q4 = 60  # ... and 60 for the last quarter of the financial year (March)

_NAME_STAMP = re.compile(r"_(\d{14})(?:_WEB)?\.xml$", re.IGNORECASE)


def stamp_date(url: str | None) -> date | None:
    """The date in an XBRL file name's `ddmmyyyyhhmmss` stamp. The time part is a 12-hour clock
    without AM/PM, so it is never read."""
    match = _NAME_STAMP.search(url or "")
    if not match:
        return None
    s = match.group(1)
    try:
        return date(int(s[4:8]), int(s[2:4]), int(s[0:2]))
    except ValueError:
        return None


def end_of(day: date) -> datetime:
    return datetime.combine(day, END_OF_DAY, tzinfo=IST)


@dataclass(frozen=True)
class FiledAt:
    at: datetime
    estimated: bool


def xbrl_at(filing: Filing) -> FiledAt | None:
    """When the filing's XBRL was filed (rules above); None when nothing dates it."""
    if filing.created_at is not None:
        return FiledAt(max(filing.created_at, filing.broadcast_at or filing.created_at), False)
    stamped = stamp_date(filing.xbrl_url)
    broadcast = filing.broadcast_at
    if stamped is not None:
        if broadcast is not None and broadcast.date() == stamped:
            return FiledAt(broadcast, False)
        return FiledAt(end_of(stamped), True)
    if broadcast is not None:
        return FiledAt(broadcast, True)  # may be a later re-broadcast: late, never early
    return None


def anchor_date(filing: Filing) -> date | None:
    """The day the filing was made, for the announcement search: the earliest of its dates."""
    days = [
        d
        for d in (
            stamp_date(filing.xbrl_url),
            filing.broadcast_at.date() if filing.broadcast_at else None,
            filing.created_at.date() if filing.created_at else None,
        )
        if d is not None
    ]
    return min(days, default=None)


def results_announcements(items: Iterable[Announcement]) -> dict[str, list[Announcement]]:
    """The results announcements among `items`, by symbol, earliest first."""
    found: dict[str, list[Announcement]] = {}
    for item in items:
        if is_results_announcement(item):
            found.setdefault(item.symbol, []).append(item)
    for listed in found.values():
        listed.sort(key=lambda i: i.at)
    return found


_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH = r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.?"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_MONTH_DAY_YEAR = re.compile(_MONTH + r"\s+" + _DAY + r",?\s+(\d{4})", re.IGNORECASE)
_DAY_MONTH_YEAR = re.compile(_DAY + r"\s+(?:of\s+)?" + _MONTH + r",?\s+(\d{4})", re.IGNORECASE)
_NUMERIC = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b")


def _quarter_end(year: int, month: int, day: int) -> date | None:
    try:
        named = date(year, month, day)
    except ValueError:
        return None
    is_end = month in (3, 6, 9, 12) and day == calendar.monthrange(year, month)[1]
    return named if is_end else None


def named_quarter_ends(text: str) -> set[date]:
    """The quarter ends (31 March, 30 June, 30 September, 31 December) an announcement's text
    names as full dates ('the period ended March 31, 2021', '31st December, 2020', '30.06.2021');
    other dates, such as the board meeting's, are left out."""
    found = set()
    for m in _MONTH_DAY_YEAR.finditer(text):
        found.add(_quarter_end(int(m[3]), _MONTHS.index(m[1].lower()) + 1, int(m[2])))
    for m in _DAY_MONTH_YEAR.finditer(text):
        found.add(_quarter_end(int(m[3]), _MONTHS.index(m[2].lower()) + 1, int(m[1])))
    for m in _NUMERIC.finditer(text):
        found.add(_quarter_end(int(m[3]), int(m[2]), int(m[1])))
    found.discard(None)
    return found  # type: ignore[return-value]


def match_announcement(
    items: Sequence[Announcement], anchor: date, period_end: date
) -> Announcement | None:
    """The results announcement of a filing for `period_end` made on `anchor`: among `items` (one
    company's results announcements, earliest first) broadcast after the period end and from two
    days before to a day after `anchor`, leaving out any that names only other quarters, the
    earliest that names `period_end`; else the earliest 'Financial Result Updates' item, else the
    earliest board outcome naming the results (texts that name no quarter)."""
    lo, hi = anchor - WINDOW_BEFORE, anchor + WINDOW_AFTER
    named: list[Announcement] = []
    unnamed: list[Announcement] = []
    for item in items:
        if not (lo <= item.at.date() <= hi and item.at.date() > period_end):
            continue
        quarters = named_quarter_ends(item.text)
        if period_end in quarters:
            named.append(item)
        elif not quarters:
            unnamed.append(item)
    if named:
        return named[0]
    for rule in ("result_updates", "board_outcome"):
        for item in unnamed:
            if is_results_announcement(item) == rule:
                return item
    return None


def pm_fix(announced: datetime, filed: FiledAt | None) -> datetime:
    """`announced`, or 12 hours later when it reads before the open and the XBRL was filed, at a
    known time on the same day, 12 hours or more after it (the one company whose announcement
    clock reads 12 hours early)."""
    if (
        filed is not None
        and not filed.estimated
        and announced.timetz().replace(tzinfo=None) < MARKET_OPEN
        and filed.at.date() == announced.date()
        and filed.at >= announced + HALF_DAY
    ):
        return announced + HALF_DAY
    return announced


@dataclass(frozen=True)
class PublicAt:
    at: datetime
    source: str  # announcement | xbrl | xbrl_date | deadline
    estimated: bool


def public_at(version: int, announced: datetime | None, filed: FiledAt | None) -> PublicAt | None:
    """When version `version` of a filing became public (rules above); None when nothing dates
    it (the filing is then excluded)."""
    filed_source = None
    if filed is not None:
        source = "xbrl_date" if filed.estimated else "xbrl"
        filed_source = PublicAt(filed.at, source, filed.estimated)
    if version > 1 or announced is None:
        return filed_source
    announced = pm_fix(announced, filed)
    if filed_source is not None and filed_source.at < announced:
        return filed_source
    return PublicAt(announced, "announcement", False)


def deadline(period_end: date) -> datetime:
    """The regulatory deadline of a quarter's results, at 23:59 IST: 45 days after the quarter
    end, 60 for the March quarter."""
    days = DEADLINE_DAYS_Q4 if period_end.month == 3 else DEADLINE_DAYS
    return datetime.combine(period_end + timedelta(days=days), time(23, 59), tzinfo=IST)


def quarter_announcement(
    items: Sequence[Announcement], period_end: date, used: set[datetime]
) -> Announcement | None:
    """The results announcement of a quarter no filing dates (a secondary-source row): among the
    results announcements after the quarter end and by its deadline plus 30 days that no other
    quarter's filing already took (`used`, by `Announcement.public`), the first that names the
    quarter, else the first 'Financial Result Updates' item that names no quarter, else the first
    board outcome naming the results that names none."""
    hi = deadline(period_end) + timedelta(days=30)
    unnamed: list[Announcement] = []
    for item in items:
        if not (period_end < item.at.date() and item.at <= hi and item.public not in used):
            continue
        quarters = named_quarter_ends(item.text)
        if period_end in quarters:
            return item
        if not quarters:
            unnamed.append(item)
    for rule in ("result_updates", "board_outcome"):
        for item in unnamed:
            if is_results_announcement(item) == rule:
                return item
    return None


def last_quarter_end(day: date) -> date:
    """The latest quarter end on or before `day`."""
    first = date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)  # the first day of day's quarter
    following = date(first.year + (first.month == 10), (first.month + 2) % 12 + 1, 1)
    return day if day == following - timedelta(days=1) else first - timedelta(days=1)


def comparative_quarter(period_end: date, listed_on: date | None) -> date:
    """The quarter whose results first made `period_end`'s figures public on NSE: `period_end`
    itself for a company listed by its end, or for the last quarter that ended before the
    listing (the first results the company files as a listed one); for an earlier quarter, the
    later of that first quarter and the quarter a year on (whose results show it as the year-ago
    comparative)."""
    if listed_on is None or period_end >= listed_on:
        return period_end
    first = last_quarter_end(listed_on - timedelta(days=1))
    if period_end >= first:
        return period_end
    return max(date(period_end.year + 1, period_end.month, period_end.day), first)
