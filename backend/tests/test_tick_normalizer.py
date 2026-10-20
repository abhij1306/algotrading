"""normalize_tick: a Fyers tick turned into the DB-keyed tick the live feed broadcasts."""

import pytest

from app.services.live.ticks import normalize_tick


@pytest.mark.parametrize(
    ("tick", "expected"),
    [
        (
            {"symbol": "NSE:SBIN-EQ", "ltp": 101.0, "chp": 1.5, "ch": 1.49, "v": 1000},
            {
                "symbol": "SBIN",
                "ltp": 101.0,
                "chp": 1.5,
                "ch": 1.49,
                "v": 1000,
                "change_pct": 1.5,
                "change": 1.49,
                "volume": 1000,
            },
        ),
        (  # no chp: derived from ltp and the previous close
            {"symbol": "NSE:SBIN-EQ", "ltp": 110.0, "prev_close_price": 100.0},
            {"symbol": "SBIN", "ltp": 110.0, "prev_close_price": 100.0, "change_pct": 10.0},
        ),
        (  # a zero previous close gives no change_pct
            {"symbol": "NSE:SBIN-EQ", "ltp": 110.0, "prev_close_price": 0},
            {"symbol": "SBIN", "ltp": 110.0, "prev_close_price": 0},
        ),
        (  # chp wins over the derived value
            {"symbol": "NSE:SBIN-EQ", "ltp": 110.0, "prev_close_price": 100.0, "chp": 2.0},
            {
                "symbol": "SBIN",
                "ltp": 110.0,
                "prev_close_price": 100.0,
                "chp": 2.0,
                "change_pct": 2.0,
            },
        ),
        (  # existing change and volume are kept
            {"symbol": "NSE:SBIN-EQ", "ch": 1.0, "change": 9.0, "v": 5, "volume": 7},
            {"symbol": "SBIN", "ch": 1.0, "change": 9.0, "v": 5, "volume": 7},
        ),
        (  # vol_traded_today is the volume fallback when v is absent
            {"symbol": "NSE:SBIN-EQ", "vol_traded_today": 42},
            {"symbol": "SBIN", "vol_traded_today": 42, "volume": 42},
        ),
        (  # v is preferred over vol_traded_today
            {"symbol": "NSE:SBIN-EQ", "v": 3, "vol_traded_today": 42},
            {"symbol": "SBIN", "v": 3, "vol_traded_today": 42, "volume": 3},
        ),
    ],
)
def test_normalize_tick(tick: dict, expected: dict) -> None:
    assert normalize_tick(tick, "SBIN") == expected


def test_normalize_tick_leaves_the_input_tick_unchanged() -> None:
    tick = {"symbol": "NSE:SBIN-EQ", "chp": 1.0, "v": 10}

    normalize_tick(tick, "SBIN")

    assert tick == {"symbol": "NSE:SBIN-EQ", "chp": 1.0, "v": 10}
