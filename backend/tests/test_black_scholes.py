"""Black-Scholes pricing, greeks and the NSE expiry clock (15:30 IST, ACT/365)."""

import math
from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.services.option_chain_service import OptionChainService
from app.services.options.black_scholes import (
    IST,
    black_scholes_call,
    black_scholes_put,
    calculate_implied_volatility,
    calculate_time_to_expiry,
    get_next_weekly_expiry,
    get_option_greeks,
    years_to_expiry,
)

HOUR_IN_YEARS = 1 / (365 * 24)

# --- golden values (Hull, Options, Futures and Other Derivatives) ----------------------


def test_hull_european_call_and_put_prices() -> None:
    # Hull example: S=42, K=40, r=10%, sigma=20%, T=0.5 -> c=4.76, p=0.81
    assert black_scholes_call(42, 40, 0.5, 0.1, 0.2) == pytest.approx(4.7594, abs=1e-4)
    assert black_scholes_put(42, 40, 0.5, 0.1, 0.2) == pytest.approx(0.8086, abs=1e-4)


def test_hull_call_greeks() -> None:
    # Hull greeks example: S=49, K=50, r=5%, sigma=20%, T=20 weeks.
    # Hull: price 2.40, delta 0.522, gamma 0.066, theta -4.31/yr, vega 12.1, rho 8.91.
    t = 20 / 52
    assert black_scholes_call(49, 50, t, 0.05, 0.2) == pytest.approx(2.4005, abs=1e-4)
    greeks = get_option_greeks(49, 50, t, 0.05, 0.2, "call")
    assert greeks == pytest.approx(
        {
            "delta": 0.5216,
            "gamma": 0.06554,
            "theta": -4.305 / 365,  # per calendar day
            "vega": 0.1211,  # per 1 vol point
            "rho": 0.08907,  # per 1 rate point
        },
        abs=1e-4,
    )


def test_put_greeks_follow_from_the_call() -> None:
    t = 20 / 52
    greeks = get_option_greeks(49, 50, t, 0.05, 0.2, "put")
    assert greeks == pytest.approx(
        {"delta": -0.4784, "gamma": 0.06554, "theta": -0.005077, "vega": 0.1211, "rho": -0.09958},
        abs=1e-4,
    )


@given(
    s=st.floats(1.0, 50_000.0),
    k=st.floats(1.0, 50_000.0),
    t=st.floats(1e-4, 3.0),
    r=st.floats(0.0, 0.15),
    sigma=st.floats(0.01, 1.5),
)
def test_put_call_parity(s: float, k: float, t: float, r: float, sigma: float) -> None:
    parity = s - k * math.exp(-r * t)
    call, put = black_scholes_call(s, k, t, r, sigma), black_scholes_put(s, k, t, r, sigma)
    assert call - put == pytest.approx(parity, abs=1e-6 * max(s, k))


def test_parity_at_nse_defaults_on_expiry_morning() -> None:
    t = years_to_expiry(date(2025, 1, 9), datetime(2025, 1, 9, 9, 30))
    call = black_scholes_call(22000, 22100, t, 0.065, 0.2)
    put = black_scholes_put(22000, 22100, t, 0.065, 0.2)
    assert call - put == pytest.approx(22000 - 22100 * math.exp(-0.065 * t), abs=1e-6)


def test_implied_volatility_round_trips() -> None:
    t = 30 / 365
    price = black_scholes_put(22000, 21800, t, 0.065, 0.17)
    iv = calculate_implied_volatility(price, 22000, 21800, t, 0.065, "put")
    assert iv == pytest.approx(0.17, abs=1e-5)


# --- expiry clock ----------------------------------------------------------------------

THURSDAY = date(2025, 1, 9)  # a Thursday weekly expiry (before NSE moved to Tuesday)


def test_morning_of_expiry_day_has_hours_left() -> None:
    assert years_to_expiry(THURSDAY, datetime(2025, 1, 9, 9, 30)) == pytest.approx(
        6 * HOUR_IN_YEARS
    )


@pytest.mark.parametrize(
    "now", [datetime(2025, 1, 9, 15, 30), datetime(2025, 1, 9, 15, 45), datetime(2025, 1, 10)]
)
def test_at_or_after_1530_ist_time_to_expiry_is_zero(now: datetime) -> None:
    assert years_to_expiry(THURSDAY, now) == 0.0


def test_aware_times_are_read_in_ist() -> None:
    # 04:00 UTC is 09:30 IST
    now = datetime(2025, 1, 9, 4, 0, tzinfo=UTC)
    assert years_to_expiry(THURSDAY, now) == pytest.approx(6 * HOUR_IN_YEARS)


def test_time_to_expiry_counts_calendar_time_not_trading_hours() -> None:
    # Regression: Monday 10:00 to Thursday 15:30 is 77.5 h = 0.00885 y (ACT/365); the old
    # code divided wall-clock hours by 6.25 * 252 trading hours and returned 0.0492 y.
    assert calculate_time_to_expiry(datetime(2025, 1, 6, 10, 0)) == pytest.approx(
        77.5 * HOUR_IN_YEARS
    )


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2025, 1, 9, 10, 0), datetime(2025, 1, 9, 15, 30, tzinfo=IST)),
        (datetime(2025, 1, 9, 15, 30), datetime(2025, 1, 16, 15, 30, tzinfo=IST)),
        (datetime(2025, 1, 10, 9, 15), datetime(2025, 1, 16, 15, 30, tzinfo=IST)),
        # NIFTY weeklies expire on Tuesday from 1 Sep 2025
        (datetime(2025, 8, 29, 10, 0), datetime(2025, 9, 2, 15, 30, tzinfo=IST)),
        (datetime(2025, 9, 9, 11, 0), datetime(2025, 9, 9, 15, 30, tzinfo=IST)),
        (datetime(2025, 9, 9, 16, 0), datetime(2025, 9, 16, 15, 30, tzinfo=IST)),
    ],
)
def test_next_weekly_expiry_includes_today_until_1530(now: datetime, expected: datetime) -> None:
    assert get_next_weekly_expiry(now) == expected


# --- at and after expiry ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("s", "call", "put", "call_delta", "put_delta"),
    [(22100, 100.0, 0.0, 1.0, 0.0), (21900, 0.0, 100.0, 0.0, -1.0), (22000, 0.0, 0.0, 0.5, -0.5)],
)
def test_expired_option_is_worth_intrinsic_with_payoff_greeks(
    s: float, call: float, put: float, call_delta: float, put_delta: float
) -> None:
    t = years_to_expiry(THURSDAY, datetime(2025, 1, 9, 15, 30))
    assert black_scholes_call(s, 22000, t, 0.065, 0.2) == call
    assert black_scholes_put(s, 22000, t, 0.065, 0.2) == put
    flat = {"gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    assert get_option_greeks(s, 22000, t, 0.065, 0.2, "call") == {"delta": call_delta, **flat}
    assert get_option_greeks(s, 22000, t, 0.065, 0.2, "put") == {"delta": put_delta, **flat}


def test_implied_volatility_after_expiry_is_zero_not_an_error() -> None:
    assert calculate_implied_volatility(5.0, 22000, 22000, 0.0, 0.065) == 0.0


# --- option chain uses the same clock --------------------------------------------------


def test_option_chain_time_to_expiry_is_ist_act_365() -> None:
    service = OptionChainService()
    now = datetime(2025, 2, 27, 4, 0, tzinfo=UTC)  # 09:30 IST on expiry day
    assert service._calculate_tte(date(2025, 2, 27), now=now) == pytest.approx(6 * HOUR_IN_YEARS)
    assert service._calculate_tte(date(2025, 2, 27), now=now + timedelta(hours=6)) == 0.0


def test_option_chain_recovers_the_volatility_a_price_was_made_with() -> None:
    # Regression: with time to expiry overstated ~5.6x, a 15% option came back at ~6% IV.
    expiry = (datetime.now(IST) + timedelta(days=10)).date()
    t = years_to_expiry(expiry, datetime.now(IST))
    ltp = black_scholes_call(22000, 22000, t, 0.065, 0.15)

    leg = OptionChainService()._create_option_leg(
        {"ltp": ltp}, 22000.0, expiry, "CE", 22000.0, include_greeks=True
    )

    assert leg.iv == pytest.approx(0.15, abs=1e-3)
    assert leg.greeks["delta"] == pytest.approx(
        get_option_greeks(22000, 22000, t, 0.065, 0.15)["delta"], abs=1e-3
    )
