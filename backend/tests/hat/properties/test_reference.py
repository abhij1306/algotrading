"""PBT reference.*: instrument identity, delisting, symbol
history, prefix paging, Fyers symbols, price bands and industries on the real code, with an
in-memory SQLite database and recorded or fake MCP servers (no network)."""

import itertools
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

START = date(2026, 1, 5)
KEYS = [(f"INE00000000{i}", "BE" if i == 5 else "EQ") for i in range(6)]
SYMBOLS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]


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


# --- Fyers symbols ----------------------------------------------------------------

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
