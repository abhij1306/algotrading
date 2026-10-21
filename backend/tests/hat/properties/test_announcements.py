"""PBT announcements.* (HAT-DATA-100..104, HAT-DEC-I6, HAT-DEC-B10): the poll cadence, GUID
dedupe, unmatched items, the 24-hour match window and the rule that text never gates, on the real
parser and store with RSS fixtures. No network."""

from collections import deque
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID

import pytest
from hat_live_support import (
    add_instrument,
    live_engine,
    rss_document,
    rss_item,
)
from hat_mcp_support import fixture_text, seed_market
from hypothesis import Phase, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.hat.data import announcements
from app.hat.data.announcements import (
    FEED_ANNOUNCEMENTS,
    FEED_RESULTS,
    FeedError,
    HttpFeedFetcher,
    attached_announcements,
    catalyst_attached,
    next_poll_at,
    parse_feed,
    poll_all,
    poll_feed,
)
from app.hat.data.sessions import IST
from app.hat.models import HatAnnouncement, HatMarket
from app.hat.ops.jobs import session_factory_for

NOW = datetime(2026, 10, 21, 10, 0, tzinfo=IST)
SYMBOL_ISINS = {"AAA": "INE000000001", "BBB": "INE000000002", "CCC": "INE000000003"}


# --- cadence (HAT-DATA-100) ---------------------------------------------------------------------


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


# --- dedupe (HAT-DATA-101) ----------------------------------------------------------------------

guids = st.sampled_from([f"g{i}" for i in range(8)])
published = st.integers(0, 72 * 60).map(lambda m: NOW - timedelta(minutes=m))
items_st = st.tuples(guids, st.sampled_from(sorted(SYMBOL_ISINS)), published)


def feed_text(items: list[tuple[str, str, datetime]], *, with_isin: bool = True) -> str:
    return rss_document(
        [
            rss_item(
                guid,
                f"{symbol} - Update",
                when,
                symbol=symbol,
                isin=SYMBOL_ISINS[symbol] if with_isin else None,
            )
            for guid, symbol, when in items
        ]
    )


_RECENT: deque[Engine] = deque()


def instrument_db() -> tuple[Engine, dict[str, UUID]]:
    """A fresh seeded in-memory database. At most two stay open (a property compares two), so a
    long Hypothesis run, or a shrink of a failing one, does not accumulate engines."""
    engine = live_engine()
    _RECENT.append(engine)
    while len(_RECENT) > 2:
        _RECENT.popleft().dispose()
    with Session(engine) as session:
        market_id = seed_market(session)
        ids = {s: add_instrument(session, market_id, s, i).id for s, i in SYMBOL_ISINS.items()}
        session.commit()
    return engine, ids


@settings(max_examples=60)
@given(polls=st.lists(st.lists(items_st, max_size=8), min_size=1, max_size=5))
def test_dedupe(polls: list[list[tuple[str, str, datetime]]]) -> None:
    """For every sequence of polls returning overlapping items (also within one document), the
    number of stored rows equals the number of distinct GUIDs."""
    engine, _ = instrument_db()
    factory = session_factory_for(engine)
    for items in polls:
        text = feed_text(items)
        poll_feed(FEED_ANNOUNCEMENTS, lambda _f, t=text: t, factory, now=NOW)
    distinct = {guid for items in polls for guid, _s, _w in items}
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(HatAnnouncement)) == len(distinct)
        stored = list(session.scalars(select(HatAnnouncement.guid)))
    assert len(stored) == len(set(stored))


def test_dedupe_recorded_fixtures() -> None:
    """The recorded Announcements feed lists one GUID twice and stores two rows; polling it again
    adds none. The Financial Results feed derives a stable GUID for an item that has none."""
    engine, _ = instrument_db()
    factory = session_factory_for(engine)
    docs = {
        FEED_ANNOUNCEMENTS: fixture_text("rss_announcements.xml"),
        FEED_RESULTS: fixture_text("rss_financial_results.xml"),
    }
    for _ in range(3):
        poll_all(list(docs), lambda feed: docs[feed], factory, now=NOW)
    with Session(engine) as session:
        by_feed = dict(
            session.execute(
                select(HatAnnouncement.feed, func.count()).group_by(HatAnnouncement.feed)
            ).all()
        )
    assert by_feed == {FEED_ANNOUNCEMENTS: 2, FEED_RESULTS: 2}


# --- unmatched items are kept (HAT-DATA-102) ----------------------------------------------------

companies = st.sampled_from([*SYMBOL_ISINS, "XXX", "YYY"])


@settings(max_examples=60)
@given(
    entries=st.lists(
        st.tuples(guids, companies, published, st.booleans()),
        unique_by=lambda e: e[0],
        max_size=8,
    ),
    listed_later=st.booleans(),
)
def test_unmatched_kept(entries: list[tuple[str, str, datetime, bool]], listed_later: bool) -> None:
    """For every polled item a row exists whether or not its company matched an instrument, its
    `raw` equals the item as received, and an unmatched item has a null `instrument_id` until a
    later poll finds its company in the instrument master."""
    engine, ids = instrument_db()
    factory = session_factory_for(engine)
    items = [
        rss_item(g, f"{c} - Update", when, symbol=c, isin=SYMBOL_ISINS.get(c) if use_isin else None)
        for g, c, when, use_isin in entries
    ]
    text = rss_document(items)
    parsed, skipped = parse_feed(FEED_ANNOUNCEMENTS, text)
    assert skipped == 0
    poll_feed(FEED_ANNOUNCEMENTS, lambda _f: text, factory, now=NOW)
    with Session(engine) as session:
        rows = {r.guid: r for r in session.scalars(select(HatAnnouncement))}
        assert set(rows) == {g for g, _c, _w, _i in entries}
        for item in parsed:
            row = rows[item.guid]
            assert row.raw == dict(item.raw)
            known = item.symbol in ids
            assert (row.instrument_id is not None) == known
            if known:
                assert row.instrument_id == ids[item.symbol]
    if listed_later:
        with Session(engine) as session:
            market = session.scalar(select(HatMarket.id))
            add_instrument(session, market, "XXX", "INE000000099")
            session.commit()
        poll_feed(FEED_ANNOUNCEMENTS, lambda _f: text, factory, now=NOW)
        with Session(engine) as session:
            for row in session.scalars(select(HatAnnouncement)):
                assert (row.instrument_id is not None) == (
                    row.raw.get("symbol") in {*SYMBOL_ISINS, "XXX"}
                )


# --- the match window (HAT-DATA-103) ------------------------------------------------------------

AT = datetime(2026, 10, 21, 9, 20, tzinfo=IST)
EDGE_SECONDS = [0, 1, -1, 24 * 3600, 24 * 3600 - 1, 24 * 3600 + 1]


@settings(max_examples=60)
@given(
    stored=st.lists(
        st.tuples(
            guids,
            st.sampled_from(sorted(SYMBOL_ISINS)),
            st.one_of(st.integers(-80 * 3600, 5 * 3600), st.sampled_from(EDGE_SECONDS)),
        ),
        unique_by=lambda e: e[0],
        max_size=10,
    ),
    within=st.integers(1, 72),
)
def test_match_window(stored: list[tuple[str, str, int]], within: int) -> None:
    """For every candidate at time t and every stored item, the item is attached if and only if
    its instrument is the candidate's and t - within_hours <= published_at <= t."""
    engine, ids = instrument_db()
    factory = session_factory_for(engine)
    items = [
        (g, s, AT - timedelta(seconds=back)) for g, s, back in stored
    ]  # `back` seconds before t; negative means published after t
    poll_feed(FEED_ANNOUNCEMENTS, lambda _f: feed_text(items), factory, now=NOW)
    with Session(engine) as session:
        for symbol, instrument_id in ids.items():
            attached = {a.guid for a in attached_announcements(session, instrument_id, AT, within)}
            expected = {g for g, s, back in stored if s == symbol and 0 <= back <= within * 3600}
            assert attached == expected
            assert catalyst_attached(session, instrument_id, AT, within) == bool(expected)


# --- text never gates (HAT-DATA-104) ------------------------------------------------------------

free_text = (
    st.text(
        st.characters(codec="utf-8", blacklist_categories=("Cs", "Cc")), min_size=1, max_size=10
    )
    .map(str.strip)
    .filter(bool)
)


# No shrink phase: a broken variant fails on nearly every example, and shrinking over arbitrary
# text then runs for minutes. The failing example is still reported, just not minimised.
@settings(
    max_examples=40,
    deadline=None,
    phases=(Phase.explicit, Phase.reuse, Phase.generate),
)
@given(
    stored=st.lists(
        st.tuples(guids, st.sampled_from(sorted(SYMBOL_ISINS)), st.integers(-3600, 80 * 3600)),
        unique_by=lambda e: e[0],
        max_size=8,
    ),
    texts_a=st.lists(st.tuples(free_text, free_text), min_size=8, max_size=8),
    texts_b=st.lists(st.tuples(free_text, free_text), min_size=8, max_size=8),
    within=st.integers(1, 72),
)
def test_text_never_gates(
    stored: list[tuple[str, str, int]],
    texts_a: list[tuple[str, str]],
    texts_b: list[tuple[str, str]],
    within: int,
) -> None:
    """For every pair of item sets identical except for title, category text and URL, the
    `catalyst_attached` outcome and the attached items are the same for every instrument."""
    outcomes = []
    for texts in (texts_a, texts_b):
        engine, ids = instrument_db()
        factory = session_factory_for(engine)
        doc = rss_document(
            [
                rss_item(
                    g,
                    texts[i][0],
                    AT - timedelta(seconds=back),
                    symbol=s,
                    isin=SYMBOL_ISINS[s],
                    url=f"https://nsearchives.example/{i}/{texts[i][1]}",
                    category=texts[i][1],
                    company=s,
                )
                for i, (g, s, back) in enumerate(stored)
            ]
        )
        poll_feed(FEED_ANNOUNCEMENTS, lambda _f, d=doc: d, factory, now=NOW)
        with Session(engine) as session:
            outcomes.append(
                {
                    symbol: (
                        catalyst_attached(session, iid, AT, within),
                        sorted(a.guid for a in attached_announcements(session, iid, AT, within)),
                    )
                    for symbol, iid in ids.items()
                }
            )
    assert outcomes[0] == outcomes[1]
