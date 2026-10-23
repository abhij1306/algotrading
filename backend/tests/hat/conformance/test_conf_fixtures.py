"""CONF: the parsers and the client's unwrapping agree with the recorded fixtures (the shapes the
real servers returned on 21 Oct 2026, and the `[A]` NSE file layouts of )."""

from datetime import date, time
from decimal import Decimal

from hat_mcp_support import ScriptedTransport, fixture_json, fixture_text

from app.hat.data.calendar import parse_nse_holidays
from app.hat.data.mcp_client import McpClient, McpError, McpTransportError, unwrap_result
from app.hat.data.reference import (
    ListedSymbol,
    build_listing_from_mcp,
    parse_equity_list_csv,
    parse_industry_csv,
    parse_price_band_csv,
)

TRADE_DATE = date(2026, 10, 19)


def test_fixtures_equity_list_and_listing() -> None:
    rows = parse_equity_list_csv(fixture_text("nse_equity_list.csv"))
    assert {(r.symbol, r.series) for r in rows} == {
        ("RELIANCE", "EQ"),
        ("TCS", "EQ"),
        ("RELIGARE", "EQ"),
        ("RELINFRA", "BE"),
    }  # the SME row and the bad-ISIN row are skipped
    reliance = next(r for r in rows if r.symbol == "RELIANCE")
    assert reliance.isin == "INE002A01018"
    assert reliance.listed_on == date(1995, 11, 29)
    assert reliance.face_value == Decimal(10)
    payload = fixture_json("mcp_cm_equity_stocks_reli.json")
    transport = ScriptedTransport([payload])
    client = McpClient(transport, sleep=lambda _s: None)
    listed = client.equity_stocks("RELI", 500)
    build = build_listing_from_mcp(
        [ListedSymbol(str(s["symbol"]), str(s["series"])) for s in listed], rows
    )
    assert build.unresolved == []
    assert len(build.securities) == 3


def test_fixtures_bands_industry_holidays() -> None:
    bands, rejected = parse_price_band_csv(fixture_text("price_band.csv"))
    assert rejected == 1  # the 15% row
    assert {(b.symbol, b.band_pct) for b in bands} == {
        ("RELIANCE", None),
        ("RELIGARE", Decimal(20)),
        ("RELINFRA", Decimal(5)),
        ("TCS", None),
    }
    industries = parse_industry_csv(fixture_text("nifty_industry.csv"))
    assert {(i.symbol, i.industry) for i in industries} == {
        ("RELIANCE", "Oil Gas & Consumable Fuels"),
        ("TCS", "Information Technology"),
    }
    days, skipped = parse_nse_holidays(fixture_text("nse_holidays.json"))
    assert skipped == 1
    assert [(d.day, d.is_trading) for d in days] == [
        (date(2026, 1, 26), False),
        (date(2026, 3, 19), False),
        (date(2026, 11, 8), True),
    ]
    assert (days[-1].open_at, days[-1].close_at) == (time(18, 15), time(19, 15))


def test_fixtures_mcp_envelopes_and_bulk_quote() -> None:
    assert unwrap_result(fixture_json("mcp_envelope_ok.json"))["count"] == 1  # type: ignore[index]
    try:
        unwrap_result(fixture_json("mcp_envelope_error.json"))
    except McpTransportError as exc:
        assert exc.code == "mcp_tool_error"
        assert "internal.example" not in str(exc)
    else:
        raise AssertionError("an isError envelope must raise")
    client = McpClient(ScriptedTransport([fixture_json("mcp_bulk_quote.json")]))
    ok = client.bulk_quote(["RELIANCE", "TCS"], TRADE_DATE)
    assert [r["symbol"] for r in ok.rows] == ["RELIANCE", "TCS"]
    assert not ok.mismatched
    stale = client.bulk_quote(["RELIANCE", "TCS"], date(2026, 10, 20))
    assert stale.rows == []
    assert len(stale.mismatched) == 2


def test_fixtures_history_mcp() -> None:
    """The recorded `get_corporate_actions`, `get_stock_history` and `get_index_history` answers
    unwrap through the client's history methods (shapes seen on 21 Oct 2026)."""
    client = McpClient(
        ScriptedTransport(
            [
                fixture_json("mcp_corporate_actions.json"),
                fixture_json("mcp_stock_history.json"),
                fixture_json("mcp_index_history.json"),
            ]
        ),
        sleep=lambda _s: None,
    )
    page = client.corporate_actions("INFY", date(2018, 6, 1), date(2018, 7, 1))
    assert not page.incomplete
    assert page.actions[0]["exDate"] == "2018-06-14"
    assert page.actions[0]["adjustmentFactor"] == 1.0
    stock = client.stock_history("RELIANCE", date(2026, 10, 15), 1)
    assert [r["date"] for r in stock][:2] == ["2026-10-13", "2026-10-14"]
    assert stock[0]["close"] == 1166.7
    index, next_end = client.index_history("India VIX", date(2026, 10, 1), 1)
    assert [r["date"] for r in index] == ["2026-09-30", "2026-10-01"]
    assert next_end == date(2026, 9, 1)


def test_fixtures_index_history_no_data() -> None:
    """The `get_index_history` answer for a window older than the server keeps (the `error` text
    of the 22 Oct 2026 build) raises `mcp_no_data`, not `mcp_bad_response`, and
    ends that index's chain without a row."""
    from app.hat.data.index_history import mcp_index_source

    reply = fixture_json("mcp_index_history_no_data.json")
    client = McpClient(ScriptedTransport([reply]), sleep=lambda _s: None)
    try:
        client.index_history("NIFTY 50", date(2023, 10, 18), 12)
    except McpError as exc:
        assert exc.code == "mcp_no_data"
    else:
        raise AssertionError("a no-data answer must raise")
    client = McpClient(ScriptedTransport([reply]), sleep=lambda _s: None)
    assert list(mcp_index_source(client)("NIFTY 50", date(2010, 1, 1), date(2023, 10, 18))) == []
