"""
Black-Scholes option pricing on the NSE expiry clock.

Conventions used throughout this module:

- Time to expiry ``t`` is in years, measured as calendar time / 365 days (ACT/365).
  Nights, weekends and holidays count, because volatility quoted as an annual figure
  (implied or historical) is annualised the same way by the option chain and Black-Scholes.
- NSE equity derivatives expire at 15:30 IST on the expiry date. Every wall-clock time is
  read in Asia/Kolkata; a naive datetime is taken to already be IST.
- At or after expiry (``t <= 0``) prices are intrinsic value and the greeks are those of
  the expired payoff: delta is 1/0 (calls) or -1/0 (puts) by moneyness, 0.5/-0.5 at the
  money, and gamma, theta, vega and rho are 0. Nothing divides by ``sqrt(t)`` there.
- Theta is per calendar day (annual theta / 365); vega and rho are per 1 percentage point.
"""

import math
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.stats import norm

IST = ZoneInfo("Asia/Kolkata")
NSE_EXPIRY_TIME = time(15, 30)
DAYS_PER_YEAR = 365.0
SECONDS_PER_YEAR = DAYS_PER_YEAR * 24 * 3600

# NIFTY weekly options expired on Thursday until NSE moved them to Tuesday on 1 Sep 2025.
TUESDAY_EXPIRY_FROM = date(2025, 9, 1)
_TUESDAY, _THURSDAY = 1, 3

# Default parameters for Indian market
DEFAULT_RISK_FREE_RATE = 0.065  # 6.5% (current RBI repo rate)
DEFAULT_VOLATILITY = 0.20  # 20% annual volatility (fallback)
DEFAULT_DTE = 7  # Days to expiration (weekly options)
NIFTY_DIVIDEND_YIELD = 0.012  # 1.2% annual dividend yield


# --- expiry clock ---------------------------------------------------------------------


def to_ist(moment: datetime) -> datetime:
    """`moment` as an IST-aware datetime; a naive datetime is taken to be IST already."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def expiry_datetime(expiry: date) -> datetime:
    """The moment an NSE contract expiring on `expiry` expires: 15:30 IST that day."""
    return datetime.combine(expiry, NSE_EXPIRY_TIME, tzinfo=IST)


def years_to_expiry(expiry: date | datetime, now: datetime) -> float:
    """Calendar time from `now` to `expiry` in years (ACT/365); 0.0 at or after expiry.

    A `date` expiry means 15:30 IST on that date.
    """
    expires_at = to_ist(expiry) if isinstance(expiry, datetime) else expiry_datetime(expiry)
    seconds = (expires_at - to_ist(now)).total_seconds()
    return max(seconds, 0.0) / SECONDS_PER_YEAR


def _weekly_expiry_weekday(day: date) -> int:
    return _TUESDAY if day >= TUESDAY_EXPIRY_FROM else _THURSDAY


def get_next_weekly_expiry(current_time: datetime) -> datetime:
    """The next NIFTY weekly expiry (15:30 IST) that has not yet passed at `current_time`.

    On the expiry day itself that is the same day until 15:30, then the following week.
    Exchange holidays (which move expiry to the previous trading day) are not modelled.
    """
    now = to_ist(current_time)
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        if day.weekday() == _weekly_expiry_weekday(day) and now < expiry_datetime(day):
            return expiry_datetime(day)
    raise AssertionError("a weekly expiry always falls within 8 days")  # pragma: no cover


def calculate_time_to_expiry(current_time: datetime) -> float:
    """Years (ACT/365) from `current_time` to the next weekly expiry; always > 0."""
    return years_to_expiry(get_next_weekly_expiry(current_time), current_time)


# --- pricing --------------------------------------------------------------------------


def _d1_d2(s: float, k: float, t: float, r: float, sigma: float) -> tuple[float, float]:
    vol_sqrt_t = sigma * math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * sigma**2) * t) / vol_sqrt_t
    return d1, d1 - vol_sqrt_t


def _is_degenerate(t: float, sigma: float) -> bool:
    """No time or no volatility left: the payoff is deterministic."""
    return t <= 0 or sigma <= 0


def black_scholes_call(s: float, k: float, t: float, r: float, sigma: float) -> float:
    """European call price. `t` in years, `r` and `sigma` annual; intrinsic at t <= 0."""
    if _is_degenerate(t, sigma):
        return max(s - k * math.exp(-r * max(t, 0.0)), 0.0)
    d1, d2 = _d1_d2(s, k, t, r, sigma)
    return float(s * norm.cdf(d1) - k * math.exp(-r * t) * norm.cdf(d2))


def black_scholes_put(s: float, k: float, t: float, r: float, sigma: float) -> float:
    """European put price. `t` in years, `r` and `sigma` annual; intrinsic at t <= 0."""
    if _is_degenerate(t, sigma):
        return max(k * math.exp(-r * max(t, 0.0)) - s, 0.0)
    d1, d2 = _d1_d2(s, k, t, r, sigma)
    return float(k * math.exp(-r * t) * norm.cdf(-d2) - s * norm.cdf(-d1))


def calculate_implied_volatility(
    option_price: float,
    s: float,
    k: float,
    t: float,
    r: float,
    option_type: str = "call",
    max_iterations: int = 100,
    tolerance: float = 1e-5,
) -> float:
    """Implied volatility by Newton-Raphson; `t` in years.

    Returns 0.0 ("no implied volatility") at or after expiry, where the price no longer
    depends on volatility, and when no volatility reproduces `option_price` (for example a
    quote below intrinsic value), so callers never mistake a failed fit for an IV.
    """
    if t <= 0:
        return 0.0
    pricer = black_scholes_call if option_type == "call" else black_scholes_put
    sigma = 0.3
    for _ in range(max_iterations):
        diff = pricer(s, k, t, r, sigma) - option_price
        if abs(diff) < tolerance:
            return float(sigma)
        d1, _d2 = _d1_d2(s, k, t, r, sigma)
        vega = s * float(norm.pdf(d1)) * math.sqrt(t)
        if vega < 1e-10:
            break
        sigma = max(sigma - diff / vega, 0.01)
    return 0.0


def calculate_atm_strike(spot_price: float, strike_interval: float = 50.0) -> float:
    """Nearest at-the-money strike on a `strike_interval` grid."""
    return round(spot_price / strike_interval) * strike_interval


def _expired_greeks(s: float, k: float, option_type: str) -> dict[str, float]:
    if s > k:
        call_delta = 1.0
    elif s < k:
        call_delta = 0.0
    else:
        call_delta = 0.5
    delta = call_delta if option_type == "call" else call_delta - 1.0
    return {"delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}


def get_option_greeks(
    s: float, k: float, t: float, r: float, sigma: float, option_type: str = "call"
) -> dict[str, float]:
    """Delta, gamma, theta (per calendar day), vega and rho (per 1 percentage point).

    `t` in years. At or after expiry, or with zero volatility, returns the greeks of the
    expired payoff (see the module docstring).
    """
    if _is_degenerate(t, sigma):
        return _expired_greeks(s, k, option_type)

    d1, d2 = _d1_d2(s, k, t, r, sigma)
    sqrt_t = math.sqrt(t)
    discount = math.exp(-r * t)
    pdf_d1 = float(norm.pdf(d1))
    decay = -s * pdf_d1 * sigma / (2 * sqrt_t)

    if option_type == "call":
        delta = float(norm.cdf(d1))
        theta_annual = decay - r * k * discount * float(norm.cdf(d2))
        rho = k * t * discount * float(norm.cdf(d2))
    else:
        delta = float(norm.cdf(d1)) - 1.0
        theta_annual = decay + r * k * discount * float(norm.cdf(-d2))
        rho = -k * t * discount * float(norm.cdf(-d2))

    return {
        "delta": delta,
        "gamma": pdf_d1 / (s * sigma * sqrt_t),
        "theta": theta_annual / DAYS_PER_YEAR,
        "vega": s * pdf_d1 * sqrt_t / 100,
        "rho": rho / 100,
    }


def calculate_historical_volatility(
    price_data: pd.DataFrame, window: int = 20, timeframe_minutes: int = 5
) -> float:
    """
    Calculate annualized historical volatility from recent price data

    Args:
        price_data: DataFrame with 'close' column
        window: Lookback period (default: 20 periods)
        timeframe_minutes: Timeframe in minutes (default: 5 for 5-min data)

    Returns:
        Annualized volatility (e.g., 0.25 for 25%)
    """
    if len(price_data) < window + 1:
        return DEFAULT_VOLATILITY

    try:
        returns = np.log(price_data["close"] / price_data["close"].shift(1))
        current_std = returns.rolling(window=window).std().iloc[-1]

        if pd.isna(current_std) or current_std == 0:
            return DEFAULT_VOLATILITY

        # For 5-min data: 75 bars per session (9:15 AM - 3:30 PM = 375 min / 5)
        periods_per_day = int(375 / timeframe_minutes)
        trading_days_per_year = 252
        annualized_vol = current_std * np.sqrt(periods_per_day * trading_days_per_year)

        # Ensure reasonable bounds (10% to 60%)
        return max(min(annualized_vol, 0.60), 0.10)

    except Exception:
        return DEFAULT_VOLATILITY


def price_synthetic_option(
    underlying_price: float,
    option_type: str = "CE",
    strike: float | None = None,
    days_to_expiry: int | None = None,
    volatility: float | None = None,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    historical_data: pd.DataFrame | None = None,
    current_time: datetime | None = None,
    apply_dividend_adjustment: bool = True,
) -> float:
    """
    Price a synthetic option using Black-Scholes.

    Args:
        underlying_price: Current price of underlying stock
        option_type: 'CE' (call) or 'PE' (put)
        strike: Strike price (if None, uses ATM)
        days_to_expiry: Calendar days until expiration (used when current_time is None)
        volatility: Annual volatility (if None, calculates from historical_data)
        risk_free_rate: Risk-free rate (default: 6.5%)
        historical_data: DataFrame with price history for volatility calculation
        current_time: Current datetime (naive = IST); prices to the next weekly expiry
        apply_dividend_adjustment: Whether to apply dividend yield adjustment

    Returns:
        Option price
    """
    if strike is None:
        strike = calculate_atm_strike(underlying_price)
    if volatility is None:
        volatility = _volatility_from_history(historical_data)
    t = _years_to_expiry(current_time, days_to_expiry)
    is_call = option_type.upper() in ["CE", "CALL"]

    if is_call:
        price = black_scholes_call(underlying_price, strike, t, risk_free_rate, volatility)
        # Dividend adjustment reduces the call price
        dividend_factor = 1 - NIFTY_DIVIDEND_YIELD * t
    else:  # PE or PUT
        price = black_scholes_put(underlying_price, strike, t, risk_free_rate, volatility)
        # Dividend adjustment increases the put price
        dividend_factor = 1 + NIFTY_DIVIDEND_YIELD * t
    if apply_dividend_adjustment:
        price = price * dividend_factor

    # Validate result - ensure no NaN or Inf values
    if not np.isfinite(price) or price <= 0:
        # Fallback to simple intrinsic value (min 1 rupee) if calculation failed
        intrinsic = underlying_price - strike if is_call else strike - underlying_price
        price = max(intrinsic, 1.0)

    return float(price)


def _volatility_from_history(historical_data: pd.DataFrame | None) -> float:
    """20-day historical volatility when there is enough history, else the default."""
    if historical_data is not None and len(historical_data) > 20:
        return calculate_historical_volatility(historical_data, window=20)
    return DEFAULT_VOLATILITY


def _years_to_expiry(current_time: datetime | None, days_to_expiry: int | None) -> float:
    """Time to expiry in years: from the clock, else from days, else the default DTE."""
    if current_time is not None:
        return calculate_time_to_expiry(current_time)
    if days_to_expiry is not None:
        return days_to_expiry / DAYS_PER_YEAR
    return DEFAULT_DTE / DAYS_PER_YEAR
