"""PBT gapscan.*: the 09:16 gap scan covers the listing, selects
by the spec's rule and records its timing, on the real code with a fake cm-market server."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any
from unittest.mock import patch

from hat_live_support import PricedListingServer, add_instrument, live_engine
from hat_mcp_support import fixture_json, seed_market
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.data import gapscan
from app.hat.data.gapscan import (
    JOB_GAPSCAN,
    gap_scan_body,
    run_gap_scan,
    scan_rows,
)
from app.hat.data.mcp_client import McpClient
from app.hat.data.sessions import IST
from app.hat.models import HatJobRun, HatPriceBand
from app.hat.ops.jobs import session_factory_for

DAY = date(2026, 10, 21)
symbol_names = st.from_regex(r"[A-C][A-C0-9&]{0,3}", fullmatch=True)
price_values = st.integers(1, 10**7).map(lambda c: Decimal(c).scaleb(-2))
# a price pair one tick either side of a threshold is as likely as an arbitrary one
gappy = st.one_of(
    st.tuples(price_values, price_values),
    price_values.map(lambda p: (p, p)),
    price_values.map(lambda p: (p * Decimal("1.1"), p)),
    price_values.map(lambda p: (p * Decimal("1.0999"), p)),
)


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


@settings(max_examples=60)
@given(
    stocks=st.lists(
        st.tuples(
            symbol_names,
            gappy,
            st.booleans(),  # passes the universe criteria
            st.one_of(st.just("missing"), BANDS),  # price-band row: absent, null (F&O) or a value
        ),
        unique_by=lambda e: e[0],
        max_size=25,
    )
)
def test_filter(stocks: list[tuple[str, tuple[Decimal, Decimal], bool, object]]) -> None:
    """For every scan result, the selected set equals {s : gap >= 10 and the universe passes and
    (band is 10 or 20, or the stock is F&O)}; every selected symbol was handed on to be
    subscribed, and a stock with no price-band row is never selected."""
    engine = live_engine()
    with Session(engine) as session:
        market_id = seed_market(session)
        passes: dict[str, bool] = {}
        for index, (symbol, _prices, universe_ok, band) in enumerate(stocks):
            instrument = add_instrument(session, market_id, symbol, f"INE{index:09d}")
            passes[symbol] = universe_ok
            if band != "missing":
                session.add(
                    HatPriceBand(
                        instrument_id=instrument.id,
                        band_pct=None if band is None else Decimal(band),
                        effective_from=DAY - timedelta(days=3),
                    )
                )
        session.commit()
    server = PricedListingServer(listing_rows([(s, "EQ", p[0], p[1]) for s, p, _, _ in stocks]))
    handed: dict[str, object] = {}
    opens: dict[str, Decimal] = {}
    body = gap_scan_body(
        client=McpClient(server, sleep=lambda _s: None),
        session_factory=session_factory_for(engine),
        trade_date=DAY,
        universe=lambda instrument: passes[instrument.symbol],
        on_candidates=lambda found, scanned: (handed.update(found), opens.update(scanned)),
    )
    body()
    expected = set()
    for symbol, (opened, prev), universe_ok, band in stocks:
        o, p = Decimal(str(float(opened))), Decimal(str(float(prev)))
        gap_ok = 100 * (Fraction(o) / Fraction(p) - 1) >= 10
        band_ok = band is None or band in (10, 20)
        if gap_ok and universe_ok and band != "missing" and band_ok:
            expected.add(symbol)
    assert set(handed) == expected
    for symbol, (opened, _prev), _u, _b in stocks:  # the listing's open reaches EP
        if symbol in expected:
            assert opens[symbol] == Decimal(str(float(opened)))


# --- timing ----------------------------------------------------------------------


@settings(max_examples=60)
@given(
    start_s=st.integers(9 * 3600 + 14 * 60, 9 * 3600 + 22 * 60),
    duration_ms=st.integers(0, 6 * 60 * 1000),
    fails=st.booleans(),
)
def test_timing_recorded(start_s: int, duration_ms: int, fails: bool) -> None:
    """Every gap-scan run, successful or failed, writes a `hat_job_run` row with `started_at` and
    `finished_at`, and a run that finishes at or after 09:20:00 IST raises a health alert."""
    engine = live_engine()
    factory = session_factory_for(engine)
    started = datetime(2026, 10, 21, tzinfo=IST) + timedelta(seconds=start_s)
    finished = started + timedelta(milliseconds=duration_ms)
    clock = iter([started.astimezone(UTC), finished.astimezone(UTC)])

    def body() -> Any:  # noqa: ANN401
        if fails:
            raise gapscan.JobError("gapscan_mcp_failed")
        return gapscan.JobResult()

    alerts: list[str] = []
    with patch("app.hat.ops.jobs.utc_now", side_effect=clock):
        run_gap_scan(body=body, trade_date=DAY, session_factory=factory, alert=alerts.append)
    with Session(engine) as session:
        row = session.scalar(select(HatJobRun).where(HatJobRun.job == JOB_GAPSCAN))
    assert row is not None
    assert row.started_at is not None
    assert row.finished_at is not None
    assert (
        row.finished_at.replace(tzinfo=UTC) - row.started_at.replace(tzinfo=UTC)
        == finished - started
    )
    late = finished >= datetime(2026, 10, 21, 9, 20, tzinfo=IST)
    assert alerts == (["gapscan_late"] if late else [])
