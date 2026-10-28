"""When a filing's figures became public (`public_at`) and when its XBRL was filed (`xbrl_at`).
Pure. Why each rule (A0 probe report, sections 5 and 6):

- The results PDF goes out as a corporate announcement ('Financial Result Updates', or a
  board-meeting outcome naming the results in the Integrated Filing era) 31 minutes to 22.5 hours
  before the XBRL, so `public_at` is the earlier of that announcement and `xbrl_at`.
- The listing's broadcast time is a later re-broadcast for many Reg 33 filings before 2022. The
  XBRL file name carries the filing date, but its time is a 12-hour clock without AM/PM: only the
  date is ever used. `xbrl_at` is the listing's broadcast time when it falls on that date, else
  the end of that date (`estimated`: an upper bound, never early). Integrated filings carry the
  document's creation time.
- A revision (version 2 on) is public from its own filing, never from the original's
  announcement.
- One company's announcement times read 12 hours early (05:08 for an evening board meeting). An
  announcement before 09:15 whose XBRL was filed, at a known time on the same day, 12 hours or
  more later is read as the PM time.
"""

from __future__ import annotations

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
        return FiledAt(filing.created_at, False)
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


def match_announcement(
    items: Sequence[Announcement], anchor: date, period_end: date
) -> Announcement | None:
    """The results announcement of a filing made on `anchor`: among `items` (one company's
    results announcements) broadcast after the period end and from two days before to a day
    after `anchor`, the earliest 'Financial Result Updates' item, else the earliest board
    outcome naming the results."""
    lo, hi = anchor - WINDOW_BEFORE, anchor + WINDOW_AFTER
    window = [i for i in items if lo <= i.at.date() <= hi and i.at.date() > period_end]
    for rule in ("result_updates", "board_outcome"):
        for item in window:
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
    """The results announcement of a quarter no filing dates (a secondary-source row): the first
    results announcement after the quarter end and by its deadline plus 30 days that no other
    quarter's filing already took (`used`)."""
    hi = deadline(period_end) + timedelta(days=30)
    for item in items:
        if period_end < item.at.date() and item.at <= hi and item.at not in used:
            return item
    return None
