"""PBT reference.* (HAT-DATA-020..026, HAT-DEC-I1): instrument identity, delisting, symbol
history, prefix paging, Fyers symbols, price bands and industries on the real code, with an
in-memory SQLite database and recorded or fake MCP servers (no network)."""

import itertools
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from hat_mcp_support import (
    ListingServer,
    equity_list_csv,
    reference_engine,
    security,
    seed_market,
)
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.data import instruments as inst
from app.hat.data.instruments import SymbolRename
from app.hat.data.mcp_client import McpClient
from app.hat.data.reference import (
    BAND_VALUES,
    BandRow,
    IndustryRow,
    Security,
    band_from_circuit_limits,
    build_listing_from_csv,
    build_listing_from_mcp,
    page_listing,
    parse_band_value,
    parse_equity_list_csv,
)
from app.hat.data.reference_jobs import reference_weekly
from app.hat.data.symbols import symbol_master
from app.hat.models import (
    HatBrokerSymbol,
    HatInstrument,
    HatInstrumentSymbolHistory,
)
from app.hat.ops.jobs import session_factory_for

START = date(2026, 1, 5)
KEYS = [(f"INE00000000{i}", "BE" if i == 5 else "EQ") for i in range(6)]
SYMBOLS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]


@st.composite
def refresh_steps(draw: st.DrawFn) -> list[list[Security]]:
    """A week-by-week series of listings: which keys are listed, under which symbols (distinct
    within a listing), with which name and face value."""
    steps = []
    for _ in range(draw(st.integers(1, 7))):
        present = draw(st.lists(st.booleans(), min_size=6, max_size=6))
        symbols = draw(st.permutations(SYMBOLS))
        names = draw(st.lists(st.sampled_from(["One", "Two"]), min_size=6, max_size=6))
        faces = draw(st.lists(st.sampled_from([1, 2, 10]), min_size=6, max_size=6))
        steps.append(
            [
                security(KEYS[i][0], KEYS[i][1], symbols[i], name=names[i], face_value=faces[i])
                for i in range(6)
                if present[i]
            ]
        )
    return steps


def new_db() -> tuple[Session, UUID]:
    engine = reference_engine()
    session = Session(engine)
    return session, seed_market(session)


def run_steps(
    session: Session, market_id: UUID, steps: list[list[Security]]
) -> Iterator[tuple[date, list[Security]]]:
    """Apply each listing in turn; yield (today, listing) after every refresh."""
    for number, listing in enumerate(steps):
        today = START + timedelta(days=7 * number)
        inst.upsert_instruments(session, market_id, listing, today=today)
        yield today, listing


def check_history_disjoint(session: Session) -> None:
    """Every instrument's symbol intervals are non-empty and never overlap."""
    ids = set(session.scalars(select(HatInstrumentSymbolHistory.instrument_id)))
    for instrument_id in ids:
        rows = inst._history(session, instrument_id)
        for row in rows:
            assert row.valid_to is None or row.valid_to > row.valid_from
        for earlier, later in zip(rows, rows[1:], strict=False):
            assert earlier.valid_to is not None, "an open interval is followed by another"
            assert earlier.valid_to <= later.valid_from


@given(steps=refresh_steps())
def test_instrument_identity_stable(steps: list[list[Security]]) -> None:
    """For every sequence of refreshes with renames, name and face-value changes, an
    instrument's id never changes while its (market, isin, series) is unchanged, and no two
    rows share a key."""
    session, market_id = new_db()
    ids: dict[tuple[str, str], object] = {}
    for _today, _listing in run_steps(session, market_id, steps):
        rows = session.scalars(select(HatInstrument)).all()
        keys = [(r.isin, r.series) for r in rows]
        assert len(keys) == len(set(keys))
        for row in rows:
            assert ids.setdefault((row.isin, row.series), row.id) == row.id
    # a repeat of the last listing changes nothing: upsert is idempotent
    before = sorted(
        (r.id, r.symbol, r.name, r.face_value, r.active, r.delisted_on)
        for r in session.scalars(select(HatInstrument))
    )
    history_before = sorted(
        (h.instrument_id, h.symbol, h.valid_from, h.valid_to)
        for h in session.scalars(select(HatInstrumentSymbolHistory))
    )
    last_day = START + timedelta(days=7 * (len(steps) - 1))
    report = inst.upsert_instruments(session, market_id, steps[-1], today=last_day)
    assert (report.created, report.updated, report.renamed, report.delisted) == (0, 0, 0, 0)
    assert before == sorted(
        (r.id, r.symbol, r.name, r.face_value, r.active, r.delisted_on)
        for r in session.scalars(select(HatInstrument))
    )
    assert history_before == sorted(
        (h.instrument_id, h.symbol, h.valid_from, h.valid_to)
        for h in session.scalars(select(HatInstrumentSymbolHistory))
    )


@given(steps=refresh_steps())
def test_delisted_never_deleted(steps: list[list[Security]]) -> None:
    """When an instrument disappears from the listing its row stays, inactive and with
    `delisted_on` set, and the number of rows never decreases."""
    session, market_id = new_db()
    seen: set[tuple[str, str]] = set()
    count = 0
    for today, listing in run_steps(session, market_id, steps):
        listed = {s.key for s in listing}
        rows = {(r.isin, r.series): r for r in session.scalars(select(HatInstrument))}
        assert len(rows) >= count
        count = len(rows)
        for key in seen - listed:
            assert key in rows
            assert rows[key].active is False
            assert rows[key].delisted_on is not None
            assert rows[key].delisted_on <= today
        for key in listed:
            assert rows[key].active is True
            assert rows[key].delisted_on is None
        seen |= listed


@given(steps=refresh_steps())
def test_symbol_history_resolves(steps: list[list[Security]]) -> None:
    """For every sequence of renames, an instrument's symbol intervals never overlap, every
    symbol it held has a history row, and a (symbol, date) resolves to at most one instrument:
    the one whose row covers that date."""
    session, market_id = new_db()
    held: dict[tuple[str, str], set[str]] = {}
    previous: list = []
    for today, listing in run_steps(session, market_id, steps):
        check_history_disjoint(session)
        by_key = {r.isin + r.series: r for r in session.scalars(select(HatInstrument))}
        for sec in listing:
            held.setdefault(sec.key, set()).add(sec.symbol)
            found = inst.resolve_symbol(session, market_id, sec.symbol, today)
            assert found is not None
            assert found.id == by_key[sec.isin + sec.series].id
        for sec in previous:  # yesterday, the symbol a rename replaced still means that stock
            found = inst.resolve_symbol(session, market_id, sec.symbol, today - timedelta(days=1))
            assert found is not None
            assert found.id == by_key[sec.isin + sec.series].id
        previous = listing
    for key, symbols in held.items():
        row = session.scalar(
            select(HatInstrument).where(
                HatInstrument.isin == key[0], HatInstrument.series == key[1]
            )
        )
        recorded = {h.symbol for h in inst._history(session, row.id)}
        assert symbols <= recorded


@given(
    chain=st.lists(st.sampled_from(SYMBOLS), min_size=2, max_size=5, unique=True),
    gaps=st.lists(st.integers(1, 400), min_size=4, max_size=4),
    listed_offset=st.integers(-800, 800),
)
def test_symbol_history_resolves_seed_idempotent(
    chain: list[str], gaps: list[int], listed_offset: int
) -> None:
    """Seeding from an injected rename source is idempotent (running it twice yields the same
    rows), keeps the intervals disjoint, and resolves each old symbol on its own dates."""
    session, market_id = new_db()
    changes = []
    renames: list[SymbolRename] = []
    day = date(2012, 1, 1)
    for old, new, gap in zip(chain, chain[1:], gaps, strict=False):
        day += timedelta(days=gap)
        changes.append((old, new, day))
        renames.append(SymbolRename(old, new, day, "INE000000001"))
    current = chain[-1]
    first_seen = changes[-1][2] + timedelta(days=listed_offset)
    inst.upsert_instruments(
        session,
        market_id,
        [security("INE000000001", "EQ", current, listed_on=first_seen)],
        today=max(first_seen, changes[-1][2]),
    )
    inst.seed_symbol_history(session, market_id, renames)

    def snapshot() -> list[tuple[str, date, date | None]]:
        return sorted(
            (h.symbol, h.valid_from, h.valid_to)
            for h in session.scalars(select(HatInstrumentSymbolHistory))
        )

    once = snapshot()
    assert inst.seed_symbol_history(session, market_id, renames) == 0
    assert snapshot() == once
    check_history_disjoint(session)
    instrument = session.scalar(select(HatInstrument))
    for old, new, effective in changes:
        before = inst.resolve_symbol(session, market_id, old, effective - timedelta(days=1))
        after = inst.resolve_symbol(session, market_id, new, effective)
        assert after is not None
        assert after.id == instrument.id
        assert before is not None  # the old symbol held until the change
        assert before.id == instrument.id


# --- prefix paging (HAT-DATA-023) -------------------------------------------------------------

listed_symbols = st.lists(
    st.tuples(st.from_regex(r"[AB&][AB&0-9]{0,4}", fullmatch=True), st.sampled_from(["EQ", "BE"])),
    unique=True,
    max_size=1500,
)


def crowded(extra: int = 0) -> list[tuple[str, str]]:
    """1,400 symbols under one prefix, so the first page and several second-level pages cap."""
    tails = ["".join(t) for n in range(1, 6) for t in itertools.product("AB&01", repeat=n)]
    return [("A", "EQ"), *(("A" + t, "EQ") for t in sorted(tails)[: 1400 + extra])]


@settings(max_examples=25)
@example(listed=crowded())
@example(listed=crowded(300))
@given(listed=listed_symbols)
def test_prefix_paging_complete(listed: list[tuple[str, str]]) -> None:
    """For every listing, of any size and spread of first letters, the paged refresh yields
    every listed symbol exactly once, and every prefix whose page reached the 500-row cap was
    split again, never accepted as complete."""
    server = ListingServer(listed)
    client = McpClient(server, sleep=lambda _s: None)
    result = page_listing(client)
    assert [(s.symbol, s.series) for s in result] == sorted(set(listed))
    asked = {prefix for prefix, _limit in server.requests}
    for prefix in asked:
        matches = sum(1 for symbol, _ in listed if symbol.startswith(prefix))
        if matches >= 500:
            assert all(prefix + c in asked for c in "AB&01")


@settings(max_examples=40)
@given(
    count=st.integers(1, 25),
    data=st.data(),
)
def test_prefix_paging_complete_csv_fallback(count: int, data: st.DataObject) -> None:
    """When the MCP refresh fails the CSV fallback yields the same instrument keys for the same
    listing (compared through the real `reference.weekly` body)."""
    symbols = data.draw(
        st.lists(
            st.from_regex(r"[A-Z]{3,6}", fullmatch=True),
            min_size=count,
            max_size=count,
            unique=True,
        )
    )
    listing = [
        security(
            f"INE{i:09d}", data.draw(st.sampled_from(["EQ", "BE"])), s, listed_on=date(2010, 1, 1)
        )
        for i, s in enumerate(symbols)
    ]
    csv_text = equity_list_csv(listing)

    def keys_after(fail: bool) -> set[tuple[str, str, str]]:
        engine = reference_engine()
        with Session(engine) as session:
            seed_market(session)
            session.commit()
        client = McpClient(
            ListingServer([(s.symbol, s.series) for s in listing], fail=fail), sleep=lambda _s: None
        )
        body = reference_weekly(
            client=client,
            equity_list=lambda: csv_text,
            industry_list=None,
            session_factory=session_factory_for(engine),
            master=symbol_master,
            today=date(2026, 10, 19),
        )
        body()
        with Session(engine) as session:
            return {(r.isin, r.series, r.symbol) for r in session.scalars(select(HatInstrument))}

    assert (
        keys_after(fail=True)
        == keys_after(fail=False)
        == {(s.isin, s.series, s.symbol) for s in listing}
    )
    parsed = parse_equity_list_csv(csv_text)
    assert {s.key for s in build_listing_from_csv(parsed).securities} == {s.key for s in listing}


def test_listing_resolves_3121_symbols() -> None:
    """Acceptance (PR 4): with fixtures, 3,121 symbols resolve to instruments, none unresolved."""
    listing = [
        security(
            f"INE{i:09d}",
            "EQ",
            "".join(chr(65 + (i // 26**k) % 26) for k in range(4)) + "X",
            listed_on=date(2015, 1, 1),
        )
        for i in range(3121)
    ]
    server = ListingServer([(s.symbol, s.series) for s in listing])
    client = McpClient(server, sleep=lambda _s: None)
    paged = page_listing(client)
    build = build_listing_from_mcp(paged, parse_equity_list_csv(equity_list_csv(listing)))
    assert len(paged) == 3121
    assert build.unresolved == []
    session, market_id = new_db()
    report = inst.upsert_instruments(session, market_id, build.securities, today=date(2026, 10, 19))
    assert report.created == 3121
    assert (
        inst.resolve_symbol(session, market_id, listing[77].symbol, date(2026, 10, 19)) is not None
    )


# --- Fyers symbols (HAT-DATA-024) ----------------------------------------------------------------

weird_symbols = st.one_of(
    st.from_regex(r"[A-Z][A-Z0-9]{1,8}", fullmatch=True),
    st.sampled_from(["M&M", "BAJAJ-AUTO", "LTIM", "NIFTY", "BANKNIFTY", "SBIN", "L&TFH", "A.B"]),
)


@given(
    entries=st.lists(
        st.tuples(weird_symbols, st.sampled_from(["EQ", "BE"]), st.booleans()),
        unique_by=lambda e: e[0] + e[1],
        max_size=25,
    )
)
def test_fyers_symbol_roundtrip(entries: list[tuple[str, str, bool]]) -> None:
    """For every active instrument with a Fyers row, `to_db(broker_symbol)` is its NSE symbol and
    no two instruments share a broker symbol; an instrument whose symbol does not map back is
    reported and given no row (never another stock's)."""
    session, market_id = new_db()
    listing = [
        security(f"INE{i:09d}", series, symbol, listed_on=date(2010, 1, 1))
        for i, (symbol, series, _active) in enumerate(entries)
    ]
    inst.upsert_instruments(session, market_id, listing, today=START)
    inst.upsert_instruments(
        session, market_id, [s for s, e in zip(listing, entries, strict=True) if e[2]], today=START
    )
    report = inst.refresh_fyers_symbols(session, symbol_master, market_id)
    rows = session.scalars(select(HatBrokerSymbol)).all()
    instruments = {r.id: r for r in session.scalars(select(HatInstrument))}
    assert len({r.broker_symbol for r in rows}) == len(rows)
    for row in rows:
        instrument = instruments[row.instrument_id]
        assert instrument.active
        assert symbol_master.to_db(row.broker_symbol) == instrument.symbol
    active = [i for i in instruments.values() if i.active]
    assert len(rows) + len(report.skipped) == len(active)

    def converts(symbol: str) -> bool:  # an oracle independent of `fyers_symbol`
        try:
            fyers = symbol_master.to_fyers(symbol)
            return fyers.endswith("-EQ") and symbol_master.to_db(fyers) == symbol
        except ValueError:
            return False

    assert sorted(report.skipped) == sorted(i.symbol for i in active if not converts(i.symbol))
    again = inst.refresh_fyers_symbols(session, symbol_master, market_id)
    assert (again.written, again.removed) == (0, 0)
    assert len(session.scalars(select(HatBrokerSymbol)).all()) == len(rows)


@given(
    entries=st.lists(
        st.tuples(
            st.booleans(),  # still listed (active)
            st.sampled_from(["fyers", "fyers", "other", "none"]),  # which broker row it has
        ),
        unique_by=None,
        max_size=15,
    )
)
def test_broker_instrument_list_comes_from_the_tables(entries: list[tuple[bool, str]]) -> None:
    """HAT-DATA-024: the instruments the Fyers adapter reports are exactly the active instruments
    that have a Fyers row, sorted, as (symbol, Fyers symbol) pairs; no index, delisted stock, other
    broker's row or unmapped symbol is in it, and the adapter returns the same list."""
    session, market_id = new_db()
    symbols = [f"SYM{i:02d}" for i in range(len(entries))]
    listing = [
        security(f"INE{i:09d}", "EQ", symbol, listed_on=date(2010, 1, 1))
        for i, symbol in enumerate(symbols)
    ]
    inst.upsert_instruments(session, market_id, listing, today=START)
    inst.upsert_instruments(
        session, market_id, [s for s, e in zip(listing, entries, strict=True) if e[0]], today=START
    )
    by_symbol = {i.symbol: i for i in session.scalars(select(HatInstrument))}
    expected = []
    for symbol, (active, broker) in zip(symbols, entries, strict=True):
        if broker != "none":
            row_broker = "fyers" if broker == "fyers" else "zerodha"
            session.add(
                HatBrokerSymbol(
                    instrument_id=by_symbol[symbol].id,
                    broker=row_broker,
                    broker_symbol=f"NSE:{symbol}-EQ",
                )
            )
        if active and broker == "fyers":
            expected.append((symbol, f"NSE:{symbol}-EQ"))
    session.flush()
    assert inst.fyers_instruments(session) == sorted(expected)

    from contextlib import nullcontext
    from unittest import mock

    from app.hat.execution.fyers_adapter import FyersHatBroker

    with mock.patch("app.database.SessionLocal", return_value=nullcontext(session)):
        found = FyersHatBroker(object()).instruments()  # type: ignore[arg-type]
    assert [(i.symbol, i.broker_symbol) for i in found] == sorted(expected)


# --- price bands and industries (HAT-DATA-025, 026) -----------------------------------------------

band_choice = st.sampled_from([Decimal(b) for b in BAND_VALUES] + [None, "skip"])


@given(
    plan=st.lists(
        st.tuples(st.integers(1, 20), st.lists(band_choice, min_size=3, max_size=3)),
        min_size=1,
        max_size=8,
    )
)
def test_current_band(plan: list[tuple[int, list[Decimal | str | None]]]) -> None:
    """For every instrument and date d, the band in force is the `band_pct` of the row with the
    greatest effective_from <= d (null meaning no band), always one of 2/5/10/20/null."""
    session, market_id = new_db()
    symbols = ["AAA", "BBB", "CCC"]
    inst.upsert_instruments(
        session,
        market_id,
        [security(f"INE00000000{i}", "EQ", s) for i, s in enumerate(symbols)],
        today=START,
    )
    ids = {r.symbol: r.id for r in session.scalars(select(HatInstrument))}
    applied: dict[str, list[tuple[date, Decimal | None]]] = {s: [] for s in symbols}
    day = START
    for gap, choices in plan:
        day += timedelta(days=gap)
        rows = [BandRow(s, "EQ", c) for s, c in zip(symbols, choices, strict=True) if c != "skip"]
        inst.load_price_bands(session, market_id, rows, day)
        for row in rows:
            applied[row.symbol].append((day, row.band_pct))
    for symbol in symbols:
        for offset in range(-2, (day - START).days + 3):
            query = START + timedelta(days=offset)
            known = [(d, b) for d, b in applied[symbol] if d <= query]
            found, band = inst.current_band(session, ids[symbol], query)
            assert found == bool(known)
            if known:
                assert band == known[-1][1]
                assert band is None or band in {Decimal(b) for b in BAND_VALUES}


BAND_TEXT = st.one_of(
    st.sampled_from(["2", "5%", " 10 ", "20", "No Band", "nil", "NA", "-", "", "15", "abc", "7.5"]),
    st.text(max_size=12),
)


@given(raw=BAND_TEXT)
def test_current_band_values_are_valid(raw: str) -> None:
    """Whatever the file says, 2/5/10/20 (with or without %) parse as that band, no-band words
    parse as no band, and anything else is rejected: nothing else is ever stored."""
    ok, band = parse_band_value(raw)
    text = raw.strip().upper().rstrip("%").strip()
    if text in {"NO BAND", "NONE", "NIL", "NA", "-", ""}:
        assert (ok, band) == (True, None)
    elif text in {"2", "5", "10", "20"}:
        assert (ok, band) == (True, Decimal(text))
    else:
        assert ok is False or band in {Decimal(b) for b in BAND_VALUES}
        assert band is None or ok


@given(
    prev=st.decimals(min_value="0.05", max_value="100000", places=2),
    up=st.decimals(min_value="-50", max_value="60", places=2),
    down=st.decimals(min_value="-50", max_value="60", places=2),
)
def test_current_band_circuit_fallback_valid(prev: Decimal, up: Decimal, down: Decimal) -> None:
    band = band_from_circuit_limits(prev, prev * (1 + up / 100), prev * (1 - down / 100))
    assert band is None or band in {Decimal(b) for b in BAND_VALUES}


@given(
    plan=st.lists(
        st.tuples(
            st.integers(1, 20), st.lists(st.sampled_from(["X", "Y", None]), min_size=3, max_size=3)
        ),
        min_size=1,
        max_size=6,
    )
)
def test_current_industry(plan: list[tuple[int, list[str | None]]]) -> None:
    """For every instrument and date d the industry used by the cap is the row with the greatest
    effective_from <= d, and an instrument with none is reported, never given a shared one."""
    session, market_id = new_db()
    symbols = ["AAA", "BBB", "CCC"]
    inst.upsert_instruments(
        session,
        market_id,
        [security(f"INE00000000{i}", "EQ", s) for i, s in enumerate(symbols)],
        today=START,
    )
    ids = {r.symbol: r.id for r in session.scalars(select(HatInstrument))}
    applied: dict[str, list[tuple[date, str]]] = {s: [] for s in symbols}
    day = START
    for gap, choices in plan:
        day += timedelta(days=gap)
        rows = [
            IndustryRow(s, "", f"sector {c}", f"industry {c}")
            for s, c in zip(symbols, choices, strict=True)
            if c is not None
        ]
        inst.load_industries(session, market_id, rows, day)
        for row in rows:
            applied[row.symbol].append((day, row.industry))
    for symbol in symbols:
        for offset in range(-1, (day - START).days + 2):
            query = START + timedelta(days=offset)
            known = [i for d, i in applied[symbol] if d <= query]
            current = inst.current_industry(session, ids[symbol], query)
            assert (current is None) == (not known)
            if known:
                assert current.industry == known[-1]
    for offset in (-1, 0, (day - START).days):
        query = START + timedelta(days=offset)
        expected = sorted(s for s in symbols if not [1 for d, _ in applied[s] if d <= query])
        assert inst.instruments_without_industry(session, market_id, query) == expected


@given(
    prev=st.decimals(min_value="1", max_value="100000", places=2),
    band=st.sampled_from([Decimal(b) for b in BAND_VALUES]),
)
def test_current_band_circuit_fallback_exact(prev: Decimal, band: Decimal) -> None:
    """Circuit limits at exactly a supported band of the previous close give that band."""
    assert band_from_circuit_limits(prev, prev * (1 + band / 100), prev * (1 - band / 100)) == band


@given(steps=refresh_steps(), data=st.data())
def test_delisted_never_deleted_listed_without_csv_row(
    steps: list[list[Security]], data: st.DataObject
) -> None:
    """A symbol the MCP lists but the CSV has no row for is reported, and an instrument already
    holding it is not delisted by that refresh."""
    session, market_id = new_db()
    for _today, _listing in run_steps(session, market_id, steps):
        pass
    active = [i for i in session.scalars(select(HatInstrument)) if i.active]
    held_back = (
        data.draw(st.lists(st.sampled_from(active), unique=True, max_size=len(active)))
        if active
        else []
    )
    keep = [a for a in active if a not in held_back]
    inst.upsert_instruments(
        session,
        market_id,
        [security(a.isin, a.series, a.symbol) for a in keep],
        today=START + timedelta(days=400),
        still_listed={(a.symbol, a.series) for a in held_back},
    )
    for instrument in held_back:
        assert instrument.active is True
        assert instrument.delisted_on is None
