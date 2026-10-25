"""Reference data on the real code: prefix paging of the listing, price bands from the file and
from circuit limits, and which instrument holds a symbol (claims and the overlap repair)."""

import itertools
import uuid
from datetime import date
from decimal import Decimal

from hat_mcp_support import (
    ListingServer,
)
from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.hat.data.mcp_client import McpClient
from app.hat.data.reference import (
    BAND_VALUES,
    band_from_circuit_limits,
    page_listing,
    parse_band_value,
)
from app.hat.data.symbol_history import (
    SKIP_NO_BARS,
    SKIP_SAME_FIRST_BAR,
    Interval,
    claim,
    overlapping_pairs,
    repair_plan,
    symbol_ids,
)

# --- prefix paging -------------------------------------------------------------

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


# --- price bands and industries -----------------------------------------------

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
    prev=st.decimals(min_value="1", max_value="100000", places=2),
    band=st.sampled_from([Decimal(b) for b in BAND_VALUES]),
)
def test_current_band_circuit_fallback_exact(prev: Decimal, band: Decimal) -> None:
    """Circuit limits at exactly a supported band of the previous close give that band."""
    assert band_from_circuit_limits(prev, prev * (1 + band / 100), prev * (1 - band / 100)) == band


def test_symbol_ids_prefer_the_active_instrument() -> None:
    """A symbol two instruments carry maps to the active one, then the lowest id, in any row
    order (`resolve_symbol`'s rule; DB audit H2: CANBK's old ISIN came last in heap order).
    Harm: live bars are stored under the dead instrument and a restart's restore loses them."""
    live, dead, other = uuid.UUID(int=9), uuid.UUID(int=1), uuid.UUID(int=5)
    rows = [(live, "CANBK", True), (dead, "CANBK", False), (other, "TCS", False)]
    for order in itertools.permutations(rows):
        assert symbol_ids(order) == {"CANBK": live, "TCS": other}
    both = [(live, "X", True), (other, "X", True)]
    assert symbol_ids(both) == symbol_ids(reversed(both)) == {"X": other}


def test_claim_closes_the_earlier_holder_and_stops_at_the_later_one() -> None:
    """A new holder of a symbol from `start` closes an earlier holder still open there and ends
    where a later holder begins; a holder that ended before, or starts the same day, is left
    alone (audit M3: an old ISIN's interval stayed open from 2011). Harm: a date after the
    change resolves the symbol to the old ISIN, and actions attach to the wrong security."""
    start = date(2014, 9, 23)
    others = [
        (date(2011, 6, 22), None),  # open: closes at start
        (date(2005, 1, 3), date(2010, 1, 4)),  # ended before: unchanged
        (date(2012, 1, 2), date(2016, 1, 1)),  # runs past start: closes at start
        (date(2020, 3, 2), None),  # a later holder: the new interval ends there
        (start, None),  # same start: a conflict, left to the database
    ]
    ends, end = claim(start, None, others)
    assert ends == [start, date(2010, 1, 4), start, None, None]
    assert end == date(2020, 3, 2)
    assert claim(start, date(2015, 1, 1), [(date(2020, 3, 2), None)]) == ([None], date(2015, 1, 1))


def test_repair_plan_closes_overlaps_at_the_next_holders_first_bar() -> None:
    """Each holder of an overlapping symbol keeps it until the next holder's first bar; a
    start before its own first bar moves to it (JSWDULUX: the new ISIN carried the company's
    2001 listing date); symbols without an overlap are untouched, and one whose holders' order
    the bars cannot give is skipped."""
    old, new, third, other = (uuid.UUID(int=i) for i in (1, 2, 3, 4))
    rows = [
        Interval(old, "SOUISPAT", date(2011, 6, 22), None),
        Interval(new, "SOUISPAT", date(2014, 9, 23), None),
        Interval(old, "AKZOINDIA", date(2011, 6, 22), date(2026, 4, 15)),
        Interval(old, "JSWDULUX", date(2026, 4, 15), None),
        Interval(new, "JSWDULUX", date(2001, 6, 28), None),
        Interval(other, "ALONE", date(2011, 6, 22), None),
        Interval(old, "TIE", date(2011, 6, 22), None),
        Interval(third, "TIE", date(2012, 1, 2), None),
        Interval(old, "NOBARS", date(2011, 6, 22), None),
        Interval(third, "NOBARS", date(2012, 1, 2), None),
    ]
    first_bars = {
        (old, "SOUISPAT"): date(2011, 6, 22),
        (new, "SOUISPAT"): date(2014, 9, 23),
        (old, "JSWDULUX"): date(2026, 4, 15),
        (new, "JSWDULUX"): date(2026, 10, 22),
        (old, "TIE"): date(2012, 1, 2),
        (third, "TIE"): date(2012, 1, 2),
        (old, "NOBARS"): date(2011, 6, 22),
    }
    assert overlapping_pairs(rows) == 4
    plan = repair_plan(rows, first_bars)
    assert plan.skipped == {"NOBARS": SKIP_NO_BARS, "TIE": SKIP_SAME_FIRST_BAR}
    assert sorted(plan.changes) == sorted(
        [
            (rows[0], rows[0]._replace(valid_to=date(2014, 9, 23))),
            (rows[3], rows[3]._replace(valid_to=date(2026, 10, 22))),
            (rows[4], rows[4]._replace(valid_from=date(2026, 10, 22))),
        ]
    )
    # an interval wholly before its holder's window is deleted
    late = [
        Interval(old, "K", date(2018, 4, 2), None),
        Interval(new, "K", date(2011, 1, 3), date(2012, 1, 2)),
        Interval(new, "K", date(2022, 6, 23), None),
    ]
    plan = repair_plan(late, {(old, "K"): date(2018, 4, 2), (new, "K"): date(2021, 8, 13)})
    assert sorted(plan.changes, key=str) == sorted(
        [(late[0], late[0]._replace(valid_to=date(2021, 8, 13))), (late[1], None)], key=str
    )
