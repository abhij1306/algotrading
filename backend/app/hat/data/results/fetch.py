"""The fetch step: everything the parse step reads, downloaded once into the raw cache.

In order: the insurers' Reg 33 listing (one bulk request), every company's announcements one day
at a time (one request a day, for `public_at`), then per covered instrument (`universe.members`,
NIFTY 500 first) and per symbol it has had, its Reg 33 and Integrated Filing listings and every
XBRL document they list for a period ending in [start, end].

Incremental and resumable: a document is never fetched again once cached (any final status,
404 included); a listing is fetched again only when older than `LISTING_MAX_AGE` (new filings
appear in it), and an announcement day only until it has been fetched after the day ended.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime

from ...time import IST
from .nse import (
    ANNOUNCEMENTS_PAGE,
    LISTING_INTEGRATED,
    LISTING_REG33,
    Client,
    Filing,
    announcements_day_url,
    filings_of,
    insurance_url,
    integrated_url,
    results_url,
)
from .raw import Entry
from .universe import Member

logger = logging.getLogger(__name__)

LISTING_MAX_AGE = timedelta(hours=20)
DAY_FINAL_AFTER = timedelta(hours=1)  # an announcement day is final once fetched this long after


def fresh(max_age: timedelta, now: datetime) -> Callable[[Entry], bool]:
    return lambda entry: entry.fetched_at >= now - max_age


def day_final(day: date) -> Callable[[Entry], bool]:
    """A day's announcements are final once fetched after the day ended (IST), plus a margin."""
    end = datetime.combine(day + timedelta(days=1), dtime(), tzinfo=IST) + DAY_FINAL_AFTER
    return lambda entry: entry.fetched_at >= end


def announcement_days(start: date, today: date) -> list[date]:
    """The days whose announcements can date a results filing for a period ending on or after
    `start`: from the day after `start` to today."""
    return [start + timedelta(days=n) for n in range(1, (today - start).days + 1)]


def listing_urls(symbol: str) -> list[tuple[str, str]]:
    return [(LISTING_REG33, results_url(symbol)), (LISTING_INTEGRATED, integrated_url(symbol))]


def in_scope(filing: Filing, start: date, end: date) -> bool:
    return filing.xbrl_url is not None and start <= filing.period_end <= end


@dataclass
class FetchReport:
    members: int = 0
    listings: int = 0
    documents: int = 0
    documents_fetched: int = 0
    announcement_days: int = 0
    statuses: dict[int, int] = field(default_factory=dict)


def fetch_nse(
    client: Client,
    members: Sequence[Member],
    start: date,
    end: date,
    today: date,
    *,
    announcements: bool = True,
) -> FetchReport:
    report = FetchReport()
    now = datetime.now(UTC)
    listing_fresh = fresh(LISTING_MAX_AGE, now)
    client.get(insurance_url(start, today), reuse=listing_fresh)
    if announcements:
        days = announcement_days(start, today)
        for n, day in enumerate(days, 1):
            answer = client.get(
                announcements_day_url(day), referer=ANNOUNCEMENTS_PAGE, reuse=day_final(day)
            )
            report.announcement_days += answer.status == 200
            if n % 100 == 0:
                logger.info("announcements %d/%d days, %d requests", n, len(days), client.requests)
    started = time.monotonic()
    seen: set[str] = set()
    for n, member in enumerate(members, 1):
        before = client.requests
        filings = _listings(client, member.symbols, listing_fresh, report)
        for filing in filings:
            if not in_scope(filing, start, end) or filing.xbrl_url in seen:
                continue
            assert filing.xbrl_url is not None
            seen.add(filing.xbrl_url)
            report.documents += 1
            if client.cache.entry(filing.xbrl_url) is None:
                client.get(filing.xbrl_url)
                report.documents_fetched += 1
        report.members = n
        elapsed = time.monotonic() - started
        logger.info(
            "%d/%d %s (nifty500=%s): %d requests (total %d), %.0f s elapsed, eta %.1f h",
            n,
            len(members),
            member.symbols[0],
            member.nifty500,
            client.requests - before,
            client.requests,
            elapsed,
            elapsed / n * (len(members) - n) / 3600,
        )
    report.statuses = dict(client.statuses)
    return report


def _listings(
    client: Client,
    symbols: Iterable[str],
    reuse: Callable[[Entry], bool],
    report: FetchReport,
) -> list[Filing]:
    answers = []
    for symbol in symbols:
        for listing, url in listing_urls(symbol):
            answer = client.get(url, reuse=reuse)
            report.listings += 1
            if answer.status == 200:
                answers.append((listing, answer.body))
    return filings_of(answers)
