"""NSE announcement feeds: parse, dedupe, match, attach (HAT-DATA-100..104).

Three RSS feeds are polled (Announcements, Financial Results, Corporate Actions). Each item is
stored once in `hat_announcement`, keyed by its GUID; an item whose company matches no instrument
is kept with a null `instrument_id` and matched on a later poll. Nothing is parsed for meaning: an
item is a catalyst for an instrument only because it is stored for that instrument and was
published within `within_hours` of the moment asked about (HAT-DATA-103/104).

No network here: the text of a feed comes from an injected `fetch`. `HttpFeedFetcher` is the one
real fetcher and is never constructed by tests. The item fields (`symbol`, `isin`, `company`,
`category` children, the `Company - Subject` title form) and the URLs are `[A]`: recorded fixtures
stand in until the first live poll validates them.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
import xml.etree.ElementTree as ET  # noqa: S405 - doctype and entities are refused first
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.hat.clock import require_aware
from app.hat.data.sessions import IST

from ..errors import HatError
from ..models import HatAnnouncement, HatInstrument, HatMarket
from ..time import as_utc
from .instruments import resolve_symbol

__all__ = [
    "FEEDS",
    "FEED_ACTIONS",
    "FEED_ANNOUNCEMENTS",
    "FEED_RESULTS",
    "INTRADAY_FEEDS",
    "WITHIN_HOURS",
    "FeedError",
    "FeedItem",
    "HttpFeedFetcher",
    "StoreReport",
    "attached_announcements",
    "catalyst_attached",
    "match_instrument",
    "next_poll_at",
    "parse_feed",
    "poll_interval",
    "rematch_unmatched",
    "store_items",
]

logger = logging.getLogger(__name__)

FEED_ANNOUNCEMENTS = "announcements"
FEED_RESULTS = "financial_results"
FEED_ACTIONS = "corporate_actions"
FEEDS = (FEED_ANNOUNCEMENTS, FEED_RESULTS, FEED_ACTIONS)
INTRADAY_FEEDS = (FEED_ANNOUNCEMENTS, FEED_RESULTS)
# [A] feed locations, validated on the first live poll.
DEFAULT_FEED_URLS = {
    FEED_ANNOUNCEMENTS: "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml",
    FEED_RESULTS: "https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml",
    FEED_ACTIONS: "https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml",
}

POLL_FAST = timedelta(minutes=2)
POLL_SLOW = timedelta(minutes=15)
FAST_FROM = time(7, 30)
FAST_UNTIL = time(15, 30)
WITHIN_HOURS = 24
FETCH_TIMEOUT_SECONDS = 20.0
MAX_FEED_BYTES = 8_000_000
_UNSAFE = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_SYMBOL = re.compile(r"^[A-Z0-9&-]{1,20}$")


class FeedError(HatError):
    """A feed could not be read. The message is a stable code, never feed text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FeedItem:
    guid: str
    feed: str
    company_raw: str
    symbol: str | None
    isin: str | None
    category: str
    title: str
    url: str
    published_at: datetime
    raw: Mapping[str, str] = field(default_factory=dict)


# --- cadence (HAT-DATA-100) ------------------------------------------------------------------


def poll_interval(at: datetime) -> timedelta:
    """2 minutes from 07:30 (inclusive) to 15:30 (exclusive) IST, 15 minutes otherwise."""
    local = require_aware(at).astimezone(IST).time()
    return POLL_FAST if FAST_FROM <= local < FAST_UNTIL else POLL_SLOW


def next_poll_at(at: datetime) -> datetime:
    """When Announcements and Financial Results are next polled after a poll at `at`."""
    return at + poll_interval(at)


# --- parsing ---------------------------------------------------------------------------------


def _text(element: ET.Element | None) -> str:
    return "" if element is None or element.text is None else element.text.strip()


def _child(item: ET.Element, name: str) -> str:
    for child in item:
        if child.tag.lower() == name:
            return _text(child)
    return ""


def _published(raw: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    # A time with no zone is NSE's own, India time.
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=IST).astimezone(UTC)


def _split_title(title: str) -> tuple[str, str]:
    company, sep, subject = title.partition(" - ")
    return (company.strip(), subject.strip()) if sep else (title.strip(), "")


def parse_feed(feed: str, text: str) -> tuple[list[FeedItem], int]:
    """Items of one RSS document, and how many items were unreadable (no title or no usable
    publication time). A document with a doctype or entity declaration is refused outright."""
    if feed not in FEEDS:
        raise FeedError("feed_unknown")
    if len(text.encode("utf-8", errors="replace")) > MAX_FEED_BYTES or _UNSAFE.search(text):
        raise FeedError("feed_unsafe")
    try:
        root = ET.fromstring(text)  # noqa: S314 - refused above if it declares a doctype
    except ET.ParseError as exc:
        raise FeedError("feed_unparseable") from exc
    items: list[FeedItem] = []
    skipped = 0
    for node in root.iter("item"):
        raw = {child.tag: _text(child) for child in node}
        title = _child(node, "title")
        published = _published(_child(node, "pubdate"))
        if not title or published is None:
            skipped += 1
            continue
        company, subject = _split_title(title)
        url = _child(node, "link")
        guid = (
            _child(node, "guid")
            or "synthetic:"
            + hashlib.sha256(
                "\0".join((feed, title, url, _child(node, "pubdate"))).encode()
            ).hexdigest()
        )
        symbol = _child(node, "symbol").upper() or None
        items.append(
            FeedItem(
                guid=guid,
                feed=feed,
                company_raw=_child(node, "company") or company,
                symbol=symbol,
                isin=_child(node, "isin").upper() or None,
                category=_child(node, "category") or subject or feed,
                title=title,
                url=url,
                published_at=published,
                raw=raw,
            )
        )
    return items, skipped


# --- matching and storing (HAT-DATA-101, 102) -----------------------------------------------


def match_instrument(
    session: Session,
    market_id: uuid.UUID,
    *,
    isin: str | None,
    symbol: str | None,
    company: str,
    on: date,
) -> uuid.UUID | None:
    """The instrument an item's company maps to, by ISIN, else by symbol, else None."""
    if isin:
        found = session.scalars(
            select(HatInstrument).where(
                HatInstrument.market_id == market_id, HatInstrument.isin == isin
            )
        ).all()
        if found:
            return min(found, key=lambda i: (not i.active, i.series != "EQ", i.id.bytes)).id
    for candidate in (symbol, company.upper()):
        if candidate and _SYMBOL.match(candidate):
            instrument = resolve_symbol(session, market_id, candidate, on)
            if instrument is not None:
                return instrument.id
    return None


@dataclass(frozen=True)
class StoreReport:
    new: int = 0
    duplicate: int = 0
    matched: int = 0  # of the new rows
    unmatched: int = 0  # of the new rows


def store_items(
    session: Session, market_id: uuid.UUID, items: Iterable[FeedItem], on: date
) -> StoreReport:
    """Store each item once by GUID. An item already stored (by an earlier poll or earlier in
    this batch) is a duplicate and changes nothing; an item matching no instrument is kept with
    a null `instrument_id`. Does not commit."""
    batch: dict[str, FeedItem] = {}
    duplicate = 0
    for item in items:
        if item.guid in batch:
            duplicate += 1
        else:
            batch[item.guid] = item
    known: set[str] = set()
    guids = list(batch)
    for start in range(0, len(guids), 500):
        known.update(
            session.scalars(
                select(HatAnnouncement.guid).where(
                    HatAnnouncement.guid.in_(guids[start : start + 500])
                )
            )
        )
    new = matched = 0
    for guid, item in batch.items():
        if guid in known:
            duplicate += 1
            continue
        instrument_id = match_instrument(
            session, market_id, isin=item.isin, symbol=item.symbol, company=item.company_raw, on=on
        )
        row = HatAnnouncement(
            id=uuid.uuid4(),
            guid=guid,
            feed=item.feed,
            instrument_id=instrument_id,
            company_raw=item.company_raw,
            category=item.category,
            title=item.title,
            url=item.url,
            published_at=item.published_at,
            raw=dict(item.raw),
        )
        try:
            with session.begin_nested():
                session.add(row)
        except IntegrityError:  # another poller stored the same GUID first
            duplicate += 1
            continue
        new += 1
        matched += instrument_id is not None
    return StoreReport(new, duplicate, matched, new - matched)


def rematch_unmatched(session: Session, market_id: uuid.UUID, on: date) -> int:
    """Try again for stored items that matched nothing (a renamed or newly listed company)."""
    changed = 0
    for row in session.scalars(
        select(HatAnnouncement).where(HatAnnouncement.instrument_id.is_(None))
    ):
        raw = row.raw if isinstance(row.raw, dict) else {}
        found = match_instrument(
            session,
            market_id,
            isin=str(raw.get("isin") or "").upper() or None,
            symbol=str(raw.get("symbol") or "").upper() or None,
            company=row.company_raw,
            on=on,
        )
        if found is not None:
            row.instrument_id = found
            changed += 1
    session.flush()
    return changed


# --- the match rule (HAT-DATA-103, 104) ------------------------------------------------------


def attached_announcements(
    session: Session, instrument_id: uuid.UUID, at: datetime, within_hours: int = WITHIN_HOURS
) -> list[HatAnnouncement]:
    """Items attached to a candidate at `at`: stored for its instrument and published in
    `[at - within_hours, at]`. The text of an item is never read."""
    at = as_utc(require_aware(at))
    return list(
        session.scalars(
            select(HatAnnouncement)
            .where(
                HatAnnouncement.instrument_id == instrument_id,
                HatAnnouncement.published_at >= at - timedelta(hours=within_hours),
                HatAnnouncement.published_at <= at,
            )
            .order_by(HatAnnouncement.published_at.desc(), HatAnnouncement.guid)
        )
    )


def catalyst_attached(
    session: Session, instrument_id: uuid.UUID, at: datetime, within_hours: int = WITHIN_HOURS
) -> bool:
    """The `catalyst_attached` outcome: at least one attached item."""
    return bool(attached_announcements(session, instrument_id, at, within_hours))


# --- fetching --------------------------------------------------------------------------------


class HttpFeedFetcher:
    """Downloads a feed. Never constructed by tests; every failure becomes a `FeedError` code."""

    def __init__(
        self,
        urls: Mapping[str, str] | None = None,
        get: Callable[..., object] | None = None,
        timeout: float = FETCH_TIMEOUT_SECONDS,
    ) -> None:
        self._urls = dict(urls or DEFAULT_FEED_URLS)
        self._get = get
        self._timeout = timeout

    def __call__(self, feed: str) -> str:
        url = self._urls.get(feed)
        if url is None:
            raise FeedError("feed_unknown")
        get = self._get
        if get is None:
            import requests  # deferred: only a real poll needs the HTTP stack

            get = requests.get
        try:
            response = get(
                url, headers={"User-Agent": "Mozilla/5.0"}, timeout=self._timeout, stream=True
            )
            if int(response.status_code) >= 400:  # type: ignore[attr-defined]
                raise FeedError("feed_http")
            # Read at most one byte past the limit, so an oversized feed is refused by
            # `parse_feed` without the whole body ever being held in memory.
            body = b"".join(
                _take(response.iter_content(chunk_size=65536), MAX_FEED_BYTES + 1)  # type: ignore[attr-defined]
            )
            return body.decode(getattr(response, "encoding", None) or "utf-8", errors="replace")
        except FeedError:
            raise
        except Exception as exc:  # network text is dropped
            raise FeedError("feed_fetch") from exc


def _take(chunks: Iterable[bytes], limit: int) -> Iterator[bytes]:
    """The chunks of a body up to `limit` bytes in all, then stop."""
    remaining = limit
    for chunk in chunks:
        yield chunk[:remaining]
        remaining -= len(chunk)
        if remaining <= 0:
            return


def poll_feed(
    feed: str,
    fetch: Callable[[str], str],
    session_factory: Callable[[], Session],
    *,
    now: datetime,
    market_code: str = "NSE",
) -> StoreReport:
    """Fetch, parse and store one feed, then rematch earlier unmatched items; commits."""
    items, skipped = parse_feed(feed, fetch(feed))
    on = as_utc(now).astimezone(IST).date()
    with session_factory() as session:
        market_id = session.scalar(select(HatMarket.id).where(HatMarket.code == market_code))
        if market_id is None:
            raise FeedError("feed_no_market")
        report = store_items(session, market_id, items, on)
        rematch_unmatched(session, market_id, on)
        session.commit()
    if skipped:
        logger.warning("feed items unreadable", extra={"job": f"announcements.{feed}"})
    return report


def poll_all(
    feeds: Sequence[str],
    fetch: Callable[[str], str],
    session_factory: Callable[[], Session],
    *,
    now: datetime,
) -> dict[str, StoreReport | str]:
    """Poll several feeds; one feed failing never stops the others (its stable code is kept)."""
    out: dict[str, StoreReport | str] = {}
    for feed in feeds:
        try:
            out[feed] = poll_feed(feed, fetch, session_factory, now=now)
        except FeedError as exc:
            out[feed] = exc.code
            logger.warning("feed poll failed", extra={"job": f"announcements.{feed}"})
    return out
