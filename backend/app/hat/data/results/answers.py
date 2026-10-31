"""What each cached URL promises, and whether a 200 answer is it (`failed`).

NSE answers with a maintenance or throttle page under status 200 ("Service Temporarily
Unavailable"), so a status alone does not say an answer is the document. Each kind of URL has its
test: an NSE API answer is JSON; an archive XBRL file has `xbrl` in its head; an archive results
page has its `tablehead` cells or NSE's page saying it has no data for the filing; an archive PDF
starts `%PDF`, a ZIP `PK`; a secondary-source company page has its profit and loss section. An
empty answer always failed. The client never caches a failed answer (`nse.Client`), and
`check_cache` marks the ones already cached with a status-0 entry, so readers skip them and the
next fetch asks again.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .raw import RawCache

OUTAGE_MARK = b"Service Temporarily Unavailable"
ARCHIVE_HOST = "nsearchives.nseindia.com"
API_PREFIX = "https://www.nseindia.com/api/"
SECONDARY_HOST = "www.screener.in"


def kind_of(url: str) -> str:
    """The kind of answer `url` promises: an NSE API endpoint's name, `xbrl`, `html`, `pdf`, `zip`,
    `secondary_page` or `other`."""
    if url.startswith(API_PREFIX):
        return "api:" + url[len(API_PREFIX) :].split("?")[0]
    path = url.split("?")[0].lower()
    if ARCHIVE_HOST in url:
        for ext, kind in ((".xml", "xbrl"), (".html", "html"), (".pdf", "pdf"), (".zip", "zip")):
            if path.endswith(ext):
                return kind
    if SECONDARY_HOST in url:
        return "secondary_page"
    return "other"


def _json(body: bytes) -> bool:
    try:
        json.loads(body)
    except ValueError:
        return False
    return True


def failed(url: str, status: int, body: bytes) -> bool:
    """Whether a 200 answer is not what `url` promises (`kind_of`)."""
    if status != 200:
        return False
    head = body[:5000]
    if not body or OUTAGE_MARK in head:
        return True
    kind = kind_of(url)
    if kind.startswith("api:"):
        return not _json(body)
    if kind == "xbrl":
        return b"xbrl" not in head.lower()
    if kind == "html":
        return not (b"tablehead" in body or b"No data available" in body)
    if kind == "pdf":
        return not body.startswith(b"%PDF")
    if kind == "zip":
        return not body.startswith(b"PK")
    if kind == "secondary_page":
        return b'id="profit-loss"' not in body
    return False


GOOD, MARKED_BEFORE, EMPTY, NOT_DOCUMENT = "good", "failed_before", "empty", "not_document"


def check_cache(cache: RawCache, *, mark: bool = True) -> dict[str, dict[str, int]]:
    """Every cached URL's latest answer, counted per kind (`kind_of`) and class: `good`,
    `http_<status>` (a non-200 answer as cached), `failed_before` (already marked), or a failed
    200 answer, `empty` or `not_document`, which `mark` marks with a status-0 entry."""
    counts: dict[str, dict[str, int]] = {}
    for url, entry in list(cache.entries().items()):
        if entry.status == 0:
            klass = MARKED_BEFORE
        elif entry.status != 200:
            klass = f"http_{entry.status}"
        else:
            body = cache.body(entry)
            klass = GOOD
            if failed(url, entry.status, body):
                klass = EMPTY if not body else NOT_DOCUMENT
                if mark:
                    cache.put(url, 0, b"")
        row = counts.setdefault(kind_of(url), {})
        row[klass] = row.get(klass, 0) + 1
    return counts
