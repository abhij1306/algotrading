"""The announcements poll: its cadence and window edges in IST, and the bounded fetch that
counts the size limit in bytes. No network."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from hat_live_support import (
    rss_document,
    rss_item,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from app.hat.data import announcements
from app.hat.data.announcements import (
    FEED_ANNOUNCEMENTS,
    FeedError,
    HttpFeedFetcher,
    next_poll_at,
    parse_feed,
)
from app.hat.time import IST

SYMBOL_ISINS = {"AAA": "INE000000001", "BBB": "INE000000002", "CCC": "INE000000003"}


# --- cadence ---------------------------------------------------------------------


@given(
    instant=st.datetimes(
        min_value=datetime(2026, 1, 1), max_value=datetime(2027, 12, 31), timezones=st.just(UTC)
    )
)
def test_poll_schedule(instant: datetime) -> None:
    """For every instant t, the next poll is 2 minutes after t when 07:30 <= t < 15:30 IST and 15
    minutes after t otherwise."""
    local = instant.astimezone(IST)
    seconds = local.hour * 3600 + local.minute * 60 + local.second
    inside = 7 * 3600 + 30 * 60 <= seconds < 15 * 3600 + 30 * 60
    assert next_poll_at(instant) - instant == timedelta(minutes=2 if inside else 15)


class _StreamedBody:
    """A response that hands out its body in chunks and records how much was read."""

    status_code = 200
    encoding = "utf-8"

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.read = 0

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        for chunk in self._chunks:
            self.read += len(chunk)
            yield chunk


ascii_chunks = st.binary(min_size=1, max_size=40).map(lambda b: bytes(32 + x % 90 for x in b))


@settings(max_examples=60)
@given(chunks=st.lists(ascii_chunks, max_size=30), cap=st.integers(1, 200))
def test_fetch_bounded(chunks: list[bytes], cap: int) -> None:
    """With a size limit of `cap`, the fetcher returns the first `cap + 1` bytes of the body at
    most, and stops reading once it has them, however large the feed. Harm: an oversized feed
    exhausts the process memory before it is refused."""
    whole = b"".join(chunks)
    body = _StreamedBody(chunks)
    with patch.object(announcements, "MAX_FEED_BYTES", cap):
        text = HttpFeedFetcher(get=lambda *_a, **_k: body)(FEED_ANNOUNCEMENTS)
    assert text.encode() == whole[: cap + 1]
    assert body.read <= cap + 1 + 40 or body.read == len(whole)


@given(words=st.integers(1, 40), extra=st.integers(-1, 1))
def test_parse_size_in_bytes(words: int, extra: int) -> None:
    """The size limit counts encoded bytes, not characters: a document of multibyte text is
    refused exactly when its UTF-8 size is over the limit. Harm: a feed far over the memory
    budget passes because its characters are fewer than its bytes."""
    doc = rss_document([rss_item("g", "é" * words, AT, symbol="AAA", isin=SYMBOL_ISINS["AAA"])])
    size = len(doc.encode("utf-8"))
    with patch.object(announcements, "MAX_FEED_BYTES", size + extra):
        if extra < 0:
            with pytest.raises(FeedError):
                parse_feed(FEED_ANNOUNCEMENTS, doc)
        else:
            parse_feed(FEED_ANNOUNCEMENTS, doc)


def test_poll_schedule_edges() -> None:
    """The window edges, in IST: 07:29:59 is slow, 07:30:00 fast, 15:29:59 fast, 15:30:00 slow."""
    day = datetime(2026, 10, 21, tzinfo=IST)
    gaps = {
        (7, 29, 59): 15,
        (7, 30, 0): 2,
        (15, 29, 59): 2,
        (15, 30, 0): 15,
    }
    for (h, m, s), minutes in gaps.items():
        at = day.replace(hour=h, minute=m, second=s)
        assert next_poll_at(at) - at == timedelta(minutes=minutes)


# --- the match window ------------------------------------------------------------

AT = datetime(2026, 10, 21, 9, 20, tzinfo=IST)
