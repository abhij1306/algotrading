"""PBT gapscan.*: the 09:16 gap scan covers the listing, selects
by the spec's rule and records its timing, on the real code with a fake cm-market server."""

from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Any

from hat_live_support import PricedListingServer
from hat_mcp_support import fixture_json
from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.hat.data.gapscan import (
    scan_rows,
)
from app.hat.data.mcp_client import McpClient

DAY = date(2026, 10, 21)
symbol_names = st.from_regex(r"[A-C][A-C0-9&]{0,3}", fullmatch=True)
price_values = st.integers(1, 10**7).map(lambda c: Decimal(c).scaleb(-2))


def listing_rows(entries: list[tuple[str, str, Decimal, Decimal]]) -> list[dict[str, Any]]:
    return [
        {"symbol": s, "series": series, "openPrice": float(o), "preClosePrice": float(p)}
        for s, series, o, p in entries
    ]


# --- covers the universe ---------------------------------------------------------


RECORDED = [
    (r["symbol"], r["series"], Decimal(str(r["openPrice"])), Decimal(str(r["preClosePrice"])))
    for r in fixture_json("mcp_cm_equity_stocks_reli.json")["stocks"]
]


@settings(max_examples=40)
@example(entries=RECORDED)
@given(
    entries=st.lists(
        st.tuples(symbol_names, st.sampled_from(["EQ", "BE"]), price_values, price_values),
        unique_by=lambda e: (e[0], e[1]),
        max_size=120,
    )
)
def test_covers_universe(entries: list[tuple[str, str, Decimal, Decimal]]) -> None:
    """For every listing, the scan evaluates every EQ symbol exactly once with gap % equal to
    100 * (open / prev_close - 1), and no symbol of another series."""
    server = PricedListingServer(listing_rows(entries))
    rows = scan_rows(McpClient(server, sleep=lambda _s: None))
    expected = sorted(s for s, series, _, _ in entries if series == "EQ")
    assert [r.symbol for r in rows] == expected
    prices_by_symbol = {s: (o, p) for s, series, o, p in entries if series == "EQ"}
    for row in rows:
        opened, prev = (Decimal(str(float(x))) for x in prices_by_symbol[row.symbol])
        assert row.gap_pct is not None
        exact = 100 * (Fraction(opened) / Fraction(prev) - 1)
        assert abs(Fraction(row.gap_pct) - exact) < Fraction(1, 10**20)


@given(
    bad=st.sampled_from([None, 0, -1.5, "n/a"]),
    where=st.sampled_from(["openPrice", "preClosePrice"]),
)
def test_covers_universe_unpriced(bad: object, where: str) -> None:
    """A symbol with a missing or non-positive price is still evaluated once, with no gap."""
    row = {"symbol": "AAA", "series": "EQ", "openPrice": 10.0, "preClosePrice": 10.0}
    row[where] = bad  # type: ignore[assignment]
    rows = scan_rows(McpClient(PricedListingServer([row]), sleep=lambda _s: None))
    assert [(r.symbol, r.gap_pct) for r in rows] == [("AAA", None)]


# --- the selection rule --------------------------------------------

BANDS = st.sampled_from([None, 2, 5, 10, 20])


# --- timing ----------------------------------------------------------------------
